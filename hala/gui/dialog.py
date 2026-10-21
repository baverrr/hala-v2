"""The settings dialog a tool opens from Dolphin or the wheel, built from its Param list."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import formats as f
from ..runner import local_inputs
from ..tools import Param, Tool, format_time, parse_time

PREVIEW_WIDTH = 220


class ToolDialog(QDialog):
    def __init__(self, tool: Tool, files: list[Path]):
        super().__init__()
        self.tool, self.files = tool, list(files)
        self.fields: dict[str, QWidget] = {}
        self.setWindowTitle(f"{tool.label} - Hala")
        self.setWindowIcon(QIcon.fromTheme("hala"))
        layout = QVBoxLayout(self)
        what = files[0].name if len(files) == 1 else f"{len(files)} files"
        intro = QLabel(f"<b>{what}</b><br>{tool.help[:1].upper() + tool.help[1:]}.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.order: QListWidget | None = None
        if tool.combine:
            layout.addWidget(QLabel("Order (drag to rearrange):"))
            self.order = QListWidget()
            self.order.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
            for p in files:
                self.order.addItem(p.name)
            self.order.setMaximumHeight(28 * min(len(files), 6) + 8)
            layout.addWidget(self.order)

        form = QFormLayout()
        for param in tool.visible_params(files):
            widget = self._field(param)
            self.fields[param.name] = widget
            label = param.label + (f" ({param.unit})" if param.unit else "")
            if isinstance(widget, QCheckBox):
                form.addRow("", widget)
            else:
                form.addRow(label + ":", widget)
                widget.setToolTip(param.help)
        layout.addLayout(form)

        self.error = QLabel()
        self.error.setStyleSheet("color: #d03030")
        self.error.hide()
        layout.addWidget(self.error)

        self.previews: tuple[QLabel, QLabel] | None = None
        if tool.name == "trim" and len(files) == 1 and f.ext_of(files[0]) in f.VIDEO_IN:
            self._add_trim_previews(layout)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText(tool.button or tool.label)
        ok.setIcon(QIcon.fromTheme(tool.icon))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setMinimumWidth(380)

    def _field(self, param: Param) -> QWidget:
        default = param.default_for(self.files)
        if param.kind == "float":
            box = QDoubleSpinBox()
            box.setDecimals(1)
            box.setRange(-100.0, 100000.0)
            box.setValue(float(default or 0))
            return box
        if param.kind == "int":
            spin = QSpinBox()
            spin.setRange(1, 999)
            spin.setValue(int(default or 1))
            return spin
        if param.kind == "bool":
            check = QCheckBox(param.label)
            check.setChecked(bool(default))
            check.setToolTip(param.help)
            return check
        if param.kind == "choice":
            combo = QComboBox()
            combo.addItems(list(param.choices))
            combo.setCurrentText(str(default))
            return combo
        line = QLineEdit()
        if param.kind == "password":
            line.setEchoMode(QLineEdit.EchoMode.Password)
            line.setPlaceholderText(param.help)
            return line
        if param.kind == "time":
            line.setText(format_time(default) if default else "")
            line.setPlaceholderText("0:00" if param.name != "end" else "the end")
        else:
            line.setText(str(default or ""))
            hint = param.placeholder(self.files) if param.placeholder else ""
            line.setPlaceholderText(f"now: {hint}" if hint else param.help)
        return line

    # --- trim previews ----------------------------------------------------------------------

    def _add_trim_previews(self, layout: QVBoxLayout) -> None:
        self._length = _duration(self.files[0])
        if self._length:
            layout.addWidget(QLabel(f"Length: {format_time(self._length)}"))
        row = QHBoxLayout()
        self.previews = (QLabel(), QLabel())
        for label in self.previews:
            label.setFixedWidth(PREVIEW_WIDTH)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row.addWidget(label)
        layout.addLayout(row)
        self._tmp = tempfile.TemporaryDirectory(prefix="hala-preview-")
        self._timer = QTimer(self, singleShot=True, interval=350)
        self._timer.timeout.connect(self._refresh_previews)
        for name in ("start", "end"):
            line = self.fields.get(name)
            if isinstance(line, QLineEdit):
                line.textChanged.connect(self._timer.start)
        self._refresh_previews()

    def _refresh_previews(self) -> None:
        assert self.previews is not None
        for name, label in zip(("start", "end"), self.previews, strict=True):
            line = self.fields[name]
            assert isinstance(line, QLineEdit)
            try:
                at = parse_time(line.text()) if line.text().strip() else (
                    0.0 if name == "start" else max(0.0, self._length - 0.1))
            except ValueError:
                label.setText("?")
                continue
            frame = Path(self._tmp.name) / f"{name}.png"
            subprocess.run(local_inputs(["ffmpeg", "-v", "error", "-y", "-ss", f"{at:.3f}",
                                         "-i", str(self.files[0]), "-frames:v", "1", "-vf",
                                         f"scale={PREVIEW_WIDTH}:-2", str(frame)]),
                           timeout=10, check=False)
            pixmap = QPixmap(str(frame))
            if pixmap.isNull():
                label.setText(f"{name}: no frame")
            else:
                label.setPixmap(pixmap)
            label.setToolTip(f"{name} at {format_time(at)}")

    # --- result -----------------------------------------------------------------------------

    def values(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, widget in self.fields.items():
            if isinstance(widget, QDoubleSpinBox | QSpinBox):
                out[name] = widget.value()
            elif isinstance(widget, QCheckBox):
                out[name] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                out[name] = widget.currentText()
            elif isinstance(widget, QLineEdit):
                # A password is taken exactly as typed: its spaces are part of it.
                masked = widget.echoMode() == QLineEdit.EchoMode.Password
                out[name] = widget.text() if masked else widget.text().strip()
        return out

    def ordered_files(self) -> list[Path]:
        if self.order is None:
            return self.files
        by_name = {p.name: p for p in self.files}
        names = [self.order.item(i).text() for i in range(self.order.count())]
        # Names can repeat (same file name in two folders): fall back to the original order.
        return [by_name[n] for n in names] if len(by_name) == len(self.files) else self.files

    def accept(self) -> None:
        try:
            self.tool.resolve(self.files, self.values())  # same parsing the tool will do
        except Exception as exc:  # ConversionError from Param.parse, shown inline
            self.error.setText(str(exc))
            self.error.show()
            return
        super().accept()


def _duration(path: Path) -> float:
    try:
        out = subprocess.run(local_inputs(["ffprobe", "-v", "error", "-show_entries",
                                           "format=duration", "-of", "csv=p=0", str(path)]),
                             capture_output=True, text=True, timeout=10).stdout
        return float(out.strip() or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0


def ask(tool: Tool, files: list[Path]) -> tuple[list[Path], dict[str, Any]] | None:
    """Show the dialog; (files in chosen order, settings), or None if cancelled."""
    app = QApplication.instance() or QApplication([])
    QApplication.setDesktopFileName("hala")
    dialog = ToolDialog(tool, files)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    result = (dialog.ordered_files(), dialog.values()) if accepted else None
    dialog.deleteLater()
    app.processEvents()
    return result


def needs_dialog(tool: Tool, files: list[Path]) -> bool:
    return bool(tool.visible_params(files)) or (tool.combine and len(files) > 1)


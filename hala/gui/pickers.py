"""The small questions an action asks before it runs: which folder, which phone."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog

from ..i18n import _
from . import translate

LAST_FOLDER = Path.home() / ".cache" / "hala" / "last-folder"


def _app() -> QApplication:
    app = QApplication.instance() or QApplication([])
    QApplication.setDesktopFileName("hala")
    assert isinstance(app, QApplication)
    translate.install(app)
    return app


def _start_folder(files: list[Path]) -> Path:
    try:
        last = Path(LAST_FOLDER.read_text().strip())
        if last.is_dir():
            return last
    except (OSError, ValueError):  # missing, unreadable or garbled: start somewhere sensible
        pass
    return files[0].parent if files else Path.home()


def pick_folder(title: str, files: list[Path]) -> Path | None:
    """A folder chosen in the file dialog (starting where the last one was), or None."""
    _app()
    chosen = QFileDialog.getExistingDirectory(None, f"{title} - Hala", str(_start_folder(files)))
    if not chosen:
        return None
    try:
        LAST_FOLDER.parent.mkdir(parents=True, exist_ok=True)
        LAST_FOLDER.write_text(chosen)
    except OSError:
        pass  # only a convenience
    return Path(chosen)


def pick_device(devices: list[tuple[str, str]]) -> str | None:
    """The id of the phone chosen from (id, name) pairs, or None."""
    _app()
    names = [name for _device_id, name in devices]
    name, ok = QInputDialog.getItem(None, _("Send to phone") + " - Hala", _("Send to:"), names,
                                    0, False)
    if not ok:
        return None
    return next(device_id for device_id, n in devices if n == name)

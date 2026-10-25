"""Qt's own text in Hala's language: the standard buttons (Cancel, Save, Discard...), the file
dialog and the right-click menus of text fields come from Qt's translations, not Hala's."""

from __future__ import annotations

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
from PySide6.QtWidgets import QApplication

from .. import i18n

_installed: list[QTranslator] = []  # kept alive: Qt doesn't own installed translators


def install(app: QApplication) -> None:
    """Load qtbase_<lang>.qm for Hala's language, once per process. Without one (English, or
    a Qt without that translation) Qt stays English, like Hala."""
    if _installed or i18n.language() == "en":
        return
    translator = QTranslator(app)
    folder = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if translator.load(QLocale(i18n.language()), "qtbase", "_", folder):
        app.installTranslator(translator)
        _installed.append(translator)

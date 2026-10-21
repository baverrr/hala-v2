"""Wayland layer-shell surfaces (edge strips, overlays) via KDE's LayerShellQt, through ctypes.

LayerShellQt has no Python bindings; its C++ API is a handful of plain member functions, so
they're called directly with the Window* as the implicit `this`. Must run before the QWindow is
shown. Without the library (or off Wayland) everything here is a no-op and callers fall back
to an ordinary window.
"""

from __future__ import annotations

import ctypes
import enum
import os

from PySide6.QtGui import QGuiApplication, QScreen, QWindow
from shiboken6 import Shiboken

LIBRARY = "libLayerShellQtInterface.so.6"


class Anchor(enum.IntFlag):  # wlr-layer-shell / LayerShellQt::Window::Anchor
    TOP = 1
    BOTTOM = 2
    LEFT = 4
    RIGHT = 8


class Layer(enum.IntEnum):
    BACKGROUND = 0
    BOTTOM = 1
    TOP = 2
    OVERLAY = 3


class Keyboard(enum.IntEnum):
    NONE = 0
    EXCLUSIVE = 1
    ON_DEMAND = 2


class _Margins(ctypes.Structure):  # QMargins: left, top, right, bottom
    _fields_ = [("left", ctypes.c_int), ("top", ctypes.c_int),
                ("right", ctypes.c_int), ("bottom", ctypes.c_int)]


_lib: ctypes.CDLL | None = None


def _load() -> ctypes.CDLL | None:
    global _lib
    if _lib is not None:
        return _lib
    if QGuiApplication.platformName() != "wayland" or os.environ.get("HALA_NO_LAYERSHELL"):
        return None
    try:
        lib = ctypes.CDLL(LIBRARY)
    except OSError:
        return None
    vp, i = ctypes.c_void_p, ctypes.c_int
    for name, args, res in (
        ("_ZN12LayerShellQt6Window3getEP7QWindow", [vp], vp),
        ("_ZN12LayerShellQt6Window10setAnchorsE6QFlagsINS0_6AnchorEE", [vp, i], None),
        ("_ZN12LayerShellQt6Window8setLayerENS0_5LayerE", [vp, i], None),
        ("_ZN12LayerShellQt6Window16setExclusiveZoneEi", [vp, i], None),
        ("_ZN12LayerShellQt6Window24setKeyboardInteractivityENS0_21KeyboardInteractivityE",
         [vp, i], None),
        ("_ZN12LayerShellQt6Window10setMarginsERK8QMargins", [vp, ctypes.POINTER(_Margins)],
         None),
        ("_ZN12LayerShellQt6Window9setScreenEP7QScreen", [vp, vp], None),
    ):
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, res
    _lib = lib
    return lib


def available() -> bool:
    return _load() is not None


def _ptr(obj: QWindow | QScreen) -> int:
    return Shiboken.getCppPointer(obj)[0]


def configure(window: QWindow, *, anchors: Anchor, layer: Layer, screen: QScreen | None = None,
              exclusive_zone: int = -1, keyboard: Keyboard = Keyboard.NONE,
              margins: tuple[int, int, int, int] = (0, 0, 0, 0)) -> bool:
    """Make window a layer surface. Returns False (and does nothing) if that isn't possible.

    exclusive_zone -1 means "don't move other surfaces aside, and ignore panels' zones".
    """
    lib = _load()
    if lib is None:
        return False
    w = lib._ZN12LayerShellQt6Window3getEP7QWindow(_ptr(window))
    if not w:
        return False
    lib._ZN12LayerShellQt6Window10setAnchorsE6QFlagsINS0_6AnchorEE(w, int(anchors))
    lib._ZN12LayerShellQt6Window8setLayerENS0_5LayerE(w, int(layer))
    lib._ZN12LayerShellQt6Window16setExclusiveZoneEi(w, exclusive_zone)
    lib._ZN12LayerShellQt6Window24setKeyboardInteractivityENS0_21KeyboardInteractivityE(
        w, int(keyboard))
    lib._ZN12LayerShellQt6Window10setMarginsERK8QMargins(w, ctypes.byref(_Margins(*margins)))
    if screen is not None:
        lib._ZN12LayerShellQt6Window9setScreenEP7QScreen(w, _ptr(screen))
    return True

"""What kind of desktop Hala is running on, and what that means for it.

Hala was built on KDE Plasma (Wayland), and that's still where everything is native: Plasma's
job tracker, Klipper, KWin's layer shell. Elsewhere it uses the freedesktop equivalents:

  * The drop zone, wheel and shelf need a layer shell to sit on a screen edge above other
    windows. Every wlroots compositor (Sway, Hyprland, Wayfire, river, labwc, niri…) and
    COSMIC offer one, through the same LayerShellQt library. GNOME's Mutter doesn't, and a
    Wayland app can't place its own windows there, so on GNOME Hala's windows run through
    XWayland, where they can (Mutter keeps X11 windows above others when asked).
  * On X11 ordinary frameless, always-on-top windows do the job.

Nothing here talks to the display server; it reads the session's environment, so it can run
before Qt starts (the platform has to be chosen by then).
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess

from .i18n import _

LAYER_SHELL_LIBRARY = "libLayerShellQtInterface.so.6"
# Desktops whose compositor has no layer shell (XDG_CURRENT_DESKTOP, lower case).
NO_LAYER_SHELL = {"gnome", "ubuntu", "pantheon", "budgie", "unity"}
# The file manager each desktop ships, for messages ("Ctrl+C in Dolphin").
FILE_MANAGERS = {"kde": "Dolphin", "gnome": "Files", "ubuntu": "Files", "x-cinnamon": "Nemo",
                 "cinnamon": "Nemo", "xfce": "Thunar", "mate": "Caja", "lxqt": "PCManFM-Qt",
                 "budgie": "Files", "pantheon": "Files", "cosmic": "Files"}


def desktops() -> list[str]:
    """XDG_CURRENT_DESKTOP's names, lower case ("ubuntu:GNOME" → ["ubuntu", "gnome"])."""
    return [d.strip().lower() for d in os.environ.get("XDG_CURRENT_DESKTOP", "").split(":")
            if d.strip()]


def is_kde() -> bool:
    return "kde" in desktops()


def is_wayland() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY")) or (
        os.environ.get("XDG_SESSION_TYPE") == "wayland")


def has_x11() -> bool:
    """An X server to fall back on (XWayland, on a Wayland session)."""
    return bool(os.environ.get("DISPLAY"))


def compositor_has_layer_shell() -> bool:
    return not NO_LAYER_SHELL.intersection(desktops())


def layer_shell_library() -> bool:
    try:
        ctypes.CDLL(LAYER_SHELL_LIBRARY)
    except OSError:
        return False
    return True


def wants_xwayland() -> bool:
    """On Wayland without a usable layer shell, edge windows can only be placed through X11."""
    if not is_wayland() or not has_x11() or os.environ.get("QT_QPA_PLATFORM"):
        return False  # X11 already, no XWayland, or the user picked Qt's platform themselves
    return not (compositor_has_layer_shell() and layer_shell_library())


def qt_args(argv0: str) -> list[str]:
    """The arguments for the QApplication of the drop zone, wheel or shelf: XWayland's
    platform when it's needed. (An argument, not QT_QPA_PLATFORM, so the editor and dialogs
    these windows start stay native Wayland apps.)"""
    return [argv0, "-platform", "xcb"] if wants_xwayland() else [argv0]


def file_manager() -> str:
    """The desktop's file manager by name, for messages; "your file manager" if unknown."""
    for name in desktops():
        if name in FILE_MANAGERS:
            return FILE_MANAGERS[name]
    return _("your file manager")


def clipboard_reader() -> list[str] | None:
    """A command that prints the clipboard's file list (text/uri-list) without needing focus,
    or None. wl-paste on Wayland; xclip or xsel on X11."""
    if is_wayland() and shutil.which("wl-paste"):
        return ["wl-paste", "--no-newline", "--type", "text/uri-list"]
    if has_x11():
        if shutil.which("xclip"):
            return ["xclip", "-out", "-selection", "clipboard", "-target", "text/uri-list"]
        if shutil.which("xsel"):  # can't ask for a type: gets text, which file managers fill
            return ["xsel", "--output", "--clipboard"]
    return None


def clipboard_writer() -> list[str] | None:
    """A command that puts its standard input on the clipboard as text, or None."""
    if is_wayland() and shutil.which("wl-copy"):
        return ["wl-copy"]
    if has_x11():
        if shutil.which("xclip"):
            return ["xclip", "-in", "-selection", "clipboard"]
        if shutil.which("xsel"):
            return ["xsel", "--input", "--clipboard"]
    return None


def error_dialog(text: str) -> bool:
    """Show a problem in a dialog of the desktop's own (for when notifications are down or
    nobody sees stderr): kdialog on Plasma, zenity on GNOME and most others, xmessage last.
    False if there's none."""
    if kdialog := shutil.which("kdialog"):
        argv = [kdialog, "--title", "Hala", "--sorry", text]
    elif zenity := shutil.which("zenity"):
        argv = [zenity, "--warning", "--title=Hala", "--no-markup", f"--text={text}"]
    elif xmessage := shutil.which("xmessage"):
        argv = [xmessage, "-center", text]
    else:
        return False
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True


def describe() -> list[str]:
    """Lines for `hala doctor`: the session as Hala sees it."""
    names = ":".join(desktops()) or _("unknown")
    kind = "Wayland" if is_wayland() else ("X11" if has_x11() else _("no display"))
    if is_wayland() and wants_xwayland():
        edge = _("XWayland windows (no layer shell here)")
    elif is_wayland():
        edge = (_("layer shell") if layer_shell_library()
                else _("plain windows (no LayerShellQt)"))
    else:
        edge = _("always-on-top windows")
    progress = _("Plasma's job tracker") if is_kde() else _("a notification")
    return [_("desktop: {names} ({kind})").format(names=names, kind=kind),
            _("edge windows: {how}").format(how=edge),
            _("progress: {how}").format(how=progress),
            _("file manager: {name}").format(name=file_manager())]

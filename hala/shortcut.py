"""The wheel's global shortcut (Meta+Shift+H: the wheel for the files last copied) off Plasma.

Plasma reads it from a kglobalaccel file (servicemenus.py, and /usr/share/kglobalaccel in the
.deb). GNOME (and Ubuntu's and Budgie's sessions, which use GNOME's settings daemon) keep
custom shortcuts in dconf, per user, so Hala adds one there: when menus are installed, and
once when the drop zone first starts on GNOME (a .deb can't write anyone's dconf). It shows up
in Settings > Keyboard > Custom Shortcuts as "Hala wheel", where it can be changed or removed;
the drop zone never puts it back (only `hala install-menus` does). Other desktops: bind
`hala wheel --clipboard` by hand.
"""

from __future__ import annotations

import ast
import shlex
import shutil
import subprocess
from pathlib import Path

from . import session
from .i18n import N_, _

SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
ENTRY_SCHEMA = f"{SCHEMA}.custom-keybinding"
PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/hala-wheel/"
BINDING = "<Super><Shift>h"
NAME = N_("Hala wheel")
GNOME_FAMILY = {"gnome", "ubuntu", "budgie", "unity"}
# Written once the shortcut has been added, so removing it in Settings sticks.
DONE = Path.home() / ".config" / "hala" / "gnome-shortcut-added"


def _gsettings(*args: str) -> str | None:
    if not shutil.which("gsettings"):
        return None
    try:
        result = subprocess.run(["gsettings", *args], capture_output=True, text=True,
                                timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _paths() -> list[str] | None:
    """The custom shortcuts' dconf paths, or None if GNOME's settings aren't there."""
    text = _gsettings("get", SCHEMA, "custom-keybindings")
    if text is None:
        return None
    text = text.removeprefix("@as ")
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None
    return [p for p in value if isinstance(p, str)] if isinstance(value, list) else None


def _set_paths(paths: list[str]) -> bool:
    return _gsettings("set", SCHEMA, "custom-keybindings", repr(paths)) is not None


def applies() -> bool:
    return bool(GNOME_FAMILY.intersection(session.desktops()))


def add_gnome(exe: str) -> bool:
    """Add (or point at exe again) the GNOME custom shortcut. False when GNOME's settings
    aren't available. A binding the user changed is kept."""
    paths = _paths()
    if paths is None:
        return False
    entry = f"{ENTRY_SCHEMA}:{PATH}"
    command = f"{shlex.quote(exe)} wheel --clipboard"  # GNOME splits it like a shell
    fresh = PATH not in paths
    if fresh and not _set_paths([*paths, PATH]):
        return False
    ok = (_gsettings("set", entry, "name", _(NAME)) is not None
          and _gsettings("set", entry, "command", command) is not None
          and (not fresh or _gsettings("set", entry, "binding", BINDING) is not None))
    if ok:
        DONE.parent.mkdir(parents=True, exist_ok=True)
        DONE.touch()
    return ok


def remove_gnome() -> bool:
    paths = _paths()
    if paths is None or PATH not in paths:
        return False
    entry = f"{ENTRY_SCHEMA}:{PATH}"
    for key in ("name", "command", "binding"):
        _gsettings("reset", entry, key)
    return _set_paths([p for p in paths if p != PATH])


def offer_once(exe: str) -> None:
    """The drop zone's first start on GNOME: add the shortcut, unless it was added before."""
    if applies() and not DONE.exists():
        add_gnome(exe)

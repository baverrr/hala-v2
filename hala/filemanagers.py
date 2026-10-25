"""Hala's right-click entries in file managers other than Dolphin.

Dolphin gets the full "Convert to" menus (servicemenus.py). The others get the two entries that
lead everywhere else: **Hala…**, which opens the wheel (every conversion, tool and action) for
the selection, and **Crop, annotate, redact…** for pictures and PDFs.

  * GNOME Files (Nautilus) and Caja: a nautilus-python / caja-python extension, which puts
    the entries straight in the right-click menu (needs python3-nautilus or python3-caja).
  * Nemo (Cinnamon): Nemo actions.
  * Thunar (Xfce): custom actions, added to the user's own uca.xml next to theirs. Only an
    existing uca.xml is changed (Thunar writes its defaults there on first start).

Every file written is marked "X-Hala-Menu", and only marked files or entries are removed.
Where the .deb already put a file in /usr/share, no per-user copy is written (it would hide or
double the system one).
"""

from __future__ import annotations

import glob
import os
import shlex
import xml.etree.ElementTree as ET
from pathlib import Path

from . import formats as f
from .i18n import N_, _

MARK = "X-Hala-Menu"
DATA = Path.home() / ".local" / "share"
SYSTEM_DATA = Path("/usr/share")
# Per user; the .deb puts the same files in /usr/share.
PYTHON_EXTENSIONS = {"nautilus": "nautilus-python/extensions", "caja": "caja-python/extensions"}
NEMO_ACTIONS = "nemo/actions"
THUNAR_UCA = Path.home() / ".config" / "Thunar" / "uca.xml"
EDITABLE = sorted({*f.IMAGE_IN, "pdf"})

# One extension serves Nautilus 3.x/4.x and Caja: get_file_items is (window, files) in the
# older APIs and (files) in Nautilus 43+, so the files are always the last argument.
PYTHON_EXTENSION = '''"""Hala's right-click entries for {app}. Written by Hala ({mark}); remove
it with `hala install-menus --remove`, or by uninstalling Hala."""

import os
import subprocess

from gi.repository import GObject, {module}

try:  # Hala's own translations, when Hala is importable here (the .deb's system Python)
    from hala.i18n import _
except ImportError:
    def _(text):
        return text

HALA = {exe!r}
EDITABLE = {editable!r}


def _run(command, paths):
    subprocess.Popen([HALA, command, *paths], stdin=subprocess.DEVNULL,
                     start_new_session=True)


class HalaMenu(GObject.GObject, {module}.MenuProvider):
    def get_file_items(self, *args):
        paths = [item.get_location().get_path() for item in args[-1]]
        if not paths or None in paths:  # not local files (trash, network places)
            return []
        wheel = {module}.MenuItem(name="Hala::wheel", label=_("Hala…"), icon="hala",
                                 tip=_("Convert, compress or edit with Hala"))
        wheel.connect("activate", lambda *_: _run("wheel", paths))
        items = [wheel]
        if all(os.path.splitext(p)[1][1:].lower() in EDITABLE for p in paths):
            edit = {module}.MenuItem(name="Hala::edit", label=_("Crop, annotate, redact…"),
                                    icon="hala")
            edit.connect("activate", lambda *_: _run("edit", paths))
            items.append(edit)
        return items
'''

NEMO_WHEEL = """[Nemo Action]
Name=Hala…
Comment=Convert, compress or edit with Hala
Exec={exe} wheel %F
Icon-Name=hala
Selection=notnone
Extensions=any;
Quote=double
{mark}=wheel
"""

NEMO_EDIT = """[Nemo Action]
Name=Crop, annotate, redact…
Comment=Edit a copy with Hala
Exec={exe} edit %F
Icon-Name=hala
Selection=notnone
Extensions={exts};
Quote=double
{mark}=edit
"""


def python_extension(app: str, exe: str) -> str:
    module = {"nautilus": "Nautilus", "caja": "Caja"}[app]
    return PYTHON_EXTENSION.format(app=module if app == "caja" else "GNOME Files",
                                   module=module, exe=exe, editable=EDITABLE, mark=MARK)


# Marked for translation: Nemo reads Name[lang]= and Comment[lang]= like a desktop entry.
DESCRIPTIONS = (N_("Convert, compress or edit with Hala"), N_("Edit a copy with Hala"))


def nemo_actions(exe: str) -> dict[str, str]:
    from .servicemenus import localize

    quoted = shlex.quote(exe)
    return {"hala-wheel.nemo_action": localize(NEMO_WHEEL.format(exe=quoted, mark=MARK)),
            "hala-edit.nemo_action": localize(NEMO_EDIT.format(
                exe=quoted, exts=";".join(EDITABLE), mark=MARK))}


def system_files(exe: str) -> dict[str, str]:
    """Paths under /usr/share (relative) and contents, for the .deb."""
    # Not "hala.py": the extension is imported under its file name, and "hala" must stay
    # Hala's own package (for its translations).
    files = {f"{folder}/hala_menu.py": python_extension(app, exe)
             for app, folder in PYTHON_EXTENSIONS.items()}
    files.update({f"{NEMO_ACTIONS}/{name}": text for name, text in nemo_actions(exe).items()})
    return files


def install(exe: str) -> list[Path]:
    """Write the per-user entries for every file manager (replacing Hala's earlier ones);
    returns what was written."""
    _remove_files()
    written = []
    for relative, text in system_files(exe).items():
        if (SYSTEM_DATA / relative).exists():
            continue  # the package's copy serves every account
        path = DATA / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        written.append(path)
    if add_thunar_actions(exe, THUNAR_UCA):
        written.append(THUNAR_UCA)
    return written


def uninstall() -> int:
    return _remove_files() + remove_thunar_actions(THUNAR_UCA)


def _remove_files() -> int:
    removed = 0
    for relative in system_files("hala"):
        path = DATA / relative
        try:
            if MARK in path.read_text(errors="replace"):
                path.unlink()
                removed += 1
        except OSError:  # not there, or not ours to remove (a folder, root's file)
            pass
    return removed


def installed_for(written: list[Path]) -> list[str]:
    """The file managers that now have Hala's entries (from written, or the package's own
    copies), by name, for `hala install-menus`; "(needs …)" where a python binding is missing."""
    found = []
    for key, name, binding in (("nautilus-python", "GNOME Files", "python3-nautilus"),
                               ("caja-python", "Caja", "python3-caja"), ("nemo", "Nemo", "")):
        ours = [r for r in system_files("hala") if r.startswith(key + "/")]
        if not any(DATA / r in written or (SYSTEM_DATA / r).exists() for r in ours):
            continue
        app = key.removesuffix("-python")
        if binding and not any(glob.glob(f"/usr/lib*/{arch}{app}/extensions-*/lib{key}.so")
                               for arch in ("", "*/")):
            name += " " + _("(needs {package})").format(package=binding)
        found.append(name)
    return found + (["Thunar"] if THUNAR_UCA in written else [])


# --- Thunar ---------------------------------------------------------------------------------

THUNAR_ACTIONS = (
    ("hala-wheel", N_("Hala…"), N_("Convert, compress or edit with Hala"), "wheel", "*"),
    ("hala-edit", N_("Crop, annotate, redact…"), N_("Edit a copy with Hala"), "edit",
     ";".join(f"*.{e}" for e in EDITABLE)),
)
# What each action is offered for (all but folders; Thunar also needs this list to show it).
THUNAR_TYPES = ("audio-files", "image-files", "other-files", "text-files", "video-files")


def _read_uca(uca: Path) -> ET.ElementTree | None:
    try:
        tree = ET.parse(uca, ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    except (OSError, ET.ParseError):
        return None  # not there yet (Thunar never started), or not ours to repair
    return tree if tree.getroot().tag == "actions" else None


def _ours(action: ET.Element) -> bool:
    return (action.findtext("unique-id") or "").startswith("hala-")


def _write_uca(tree: ET.ElementTree, uca: Path) -> None:
    ET.indent(tree, space="\t")
    target = uca.resolve()  # a uca.xml linked from a dotfiles folder stays a link
    tmp = target.with_name(target.name + ".hala-tmp")
    tree.write(tmp, encoding="UTF-8", xml_declaration=True)
    os.replace(tmp, target)


def add_thunar_actions(exe: str, uca: Path) -> bool:
    tree = _read_uca(uca)
    if tree is None:
        return False
    root = tree.getroot()
    for action in [a for a in root.findall("action") if _ours(a)]:
        root.remove(action)
    for uid, name, description, command, patterns in THUNAR_ACTIONS:
        action = ET.SubElement(root, "action")
        for tag, text in (("icon", "hala"), ("name", _(name)), ("submenu", ""),
                          ("unique-id", uid), ("command", f"{shlex.quote(exe)} {command} %F"),
                          ("description", f"{_(description)} ({MARK})"), ("range", "*"),
                          ("patterns", patterns)):
            ET.SubElement(action, tag).text = text
        if command == "wheel":
            ET.SubElement(action, "directories")
        for kind in THUNAR_TYPES:
            ET.SubElement(action, kind)
    _write_uca(tree, uca)
    return True


def remove_thunar_actions(uca: Path) -> int:
    tree = _read_uca(uca)
    if tree is None:
        return 0
    root = tree.getroot()
    ours = [a for a in root.findall("action") if _ours(a)]
    for action in ours:
        root.remove(action)
    if ours:
        _write_uca(tree, uca)
    return len(ours)

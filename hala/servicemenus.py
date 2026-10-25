"""Dolphin "Convert to" right-click menus, generated from the conversion table.

Two kinds of menu file:
  * one per input format, shown for a single selected file, listing exactly its targets;
  * one per format family (all images, all audio…), shown when several files are selected,
    listing the whole family's targets (files already in the chosen format are skipped).

Dolphin matches MIME types by inheritance (a .csv is also text/plain, a .py is too), so each
file lists the more specific types under ExcludeServiceTypes: other menus' types, and every
type we can't convert. The engines go by extension, so a menu shown for a type outside the
table would only lead to an error.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path

from . import formats as f
from . import i18n
from .i18n import N_
from .registry import ANY, ROUTES, targets_for
from .tools import Tool, tools

MENU_DIR = Path.home() / ".local" / "share" / "kio" / "servicemenus"
APP_DIR = Path.home() / ".local" / "share" / "applications"
ICON_DIR = Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps"
# Where KDE's global-shortcut service finds default shortcuts for apps that aren't running.
SHORTCUT_DIR = Path.home() / ".local" / "share" / "kglobalaccel"
SHORTCUT_FILE = "hala-wheel.desktop"
DEFAULT_SHORTCUT = "Meta+Shift+H"
ICON = Path(__file__).parent / "data" / "hala.svg"
PREFIX = "hala-"
MIME_DIRS = [Path("/usr/share/mime"), Path.home() / ".local" / "share" / "mime"]
# Ark already offers Compress for any file, so plain files don't get zip/tar.gz/7z here;
# archives keep them as re-pack targets.
PACK = set(f.PACK_OUT)
# When an extension's top glob weights tie, prefer the type in the family's own media tree.
FAMILY_MEDIA = {"image": "image/", "video": "video/", "audio": "audio/"}

# Text in the menu files and entries below, translated into every language Hala has: each
# Name=English line gets a Name[de]=... line after it (see translated() and localize()).
DESKTOP_TEXT = (
    N_("Convert to"), N_("Hala tools"), N_("Crop, annotate, redact…"), N_("Hala…"),
    N_("Put on shelf"), N_("File Converter"),
    N_("Convert, compress and edit files locally: drag them to the screen edge"),
    # Translators: search words for the application menu, separated (and ended) by ";".
    N_("convert;converter;compress;pdf;image;video;audio;"),
    N_("Open the wheel for copied files"), N_("Open the shelf"), N_("Hala wheel"),
    N_("Open the format wheel for the files last copied with Ctrl+C"),
)
LOCALIZED_KEYS = ("Name", "GenericName", "Comment", "Keywords", "X-KDE-Submenu")

ICONS = {f.FOLDER: "archive-extract", "pdf": "application-pdf", "zip": "application-zip",
         "tar.gz": "application-x-compressed-tar", "7z": "application-x-7z-compressed",
         "txt": "text-plain", "md": "text-markdown", "html": "text-html"}


class MimeDb:
    """Just enough of shared-mime-info: weighted extension globs and the subclass tree."""

    def __init__(self, dirs: Iterable[Path] = MIME_DIRS):
        self._globs: dict[str, dict[str, int]] = {}  # ext -> {mime: weight}
        self._parents: dict[str, set[str]] = {}
        for d in dirs:
            self._read_globs(d / "globs2")
            self._read_subclasses(d / "subclasses")

    def _read_globs(self, path: Path) -> None:
        if not path.exists():
            return
        for line in path.read_text().splitlines():
            if line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) < 3 or not parts[2].startswith("*.") or not parts[0].isdigit():
                continue
            ext = parts[2][2:].lower()
            if any(ch in ext for ch in "*?["):
                continue
            weights = self._globs.setdefault(ext, {})
            weights[parts[1]] = max(weights.get(parts[1], 0), int(parts[0]))

    def _read_subclasses(self, path: Path) -> None:
        if path.exists():
            for line in path.read_text().splitlines():
                child, _sep, parent = line.partition(" ")
                if parent:
                    self._parents.setdefault(child, set()).add(parent)

    def all_types(self) -> set[str]:
        return {m for w in self._globs.values() for m in w} | set(self._parents)

    def inherits(self, mime: str, ancestor: str) -> bool:
        seen, todo = set(), [mime]
        while todo:
            current = todo.pop()
            if current == ancestor:
                return True
            if current not in seen:
                seen.add(current)
                todo.extend(self._parents.get(current, ()))
        return False

    def mimes_for(self, ext: str) -> list[str]:
        """MIME types for one of our normalized extensions, including its aliases (jpeg, tif…).

        Only each spelling's highest-weight globs count: "*.ass" is also a weight-10 glob for
        audio/aac, which would put the subtitle menu on every AAC file.
        """
        spellings = [ext, *(alias for alias, canon in f.ALIASES.items() if canon == ext)]
        media = next((FAMILY_MEDIA[fam] for fam, exts in f.FAMILIES.items()
                      if ext in exts and fam in FAMILY_MEDIA), None)
        found: list[str] = []
        for spelling in spellings:
            weights = self._globs.get(spelling, {})
            if not weights:
                continue
            top = max(weights.values())
            best = [m for m, w in weights.items() if w == top]
            if media and any(m.startswith(media) for m in best):
                best = [m for m in best if m.startswith(media)]  # .ts: video, not Qt Linguist
            found += [m for m in best if m not in found]
        return found


def _icon(target: str, db: MimeDb) -> str:
    if target in ICONS:
        return ICONS[target]
    mimes = db.mimes_for(target)
    return mimes[0].replace("/", "-") if mimes else "document-export"


def _menu_targets(src_exts: Iterable[str]) -> list[str]:
    exts = list(src_exts)
    is_archive = all(e in f.ARCHIVE_IN for e in exts)
    seen: dict[str, None] = {}
    for ext in exts:
        for target in targets_for(ext):
            if is_archive or target not in PACK:
                seen.setdefault(target, None)
    return list(seen)


def _desktop_file(name: str, mimes: list[str], excludes: list[str], targets: list[str],
                  db: MimeDb, exe: str, single: bool) -> str:
    actions = [f"to_{t.replace('.', '_')}" for t in targets]
    lines = [
        "[Desktop Entry]", "Type=Service", f"X-Hala-Menu={name}",
        "X-KDE-ServiceTypes=KonqPopupMenu/Plugin", "X-KDE-Protocols=file",
        f"MimeType={';'.join(mimes)};", *translated("X-KDE-Submenu", "Convert to"),
        "Icon=hala", f"Actions={';'.join(actions)};",
        "X-KDE-RequiredNumberOfUrls=1" if single else "X-KDE-MinNumberOfUrls=2",
    ]
    if excludes:
        lines.append(f"ExcludeServiceTypes={';'.join(excludes)};")
    for action, target in zip(actions, targets, strict=True):
        context = "format" if target in f.LABELS else None
        lines += ["", f"[Desktop Action {action}]",
                  *translated("Name", f.english_label(target), context=context),
                  f"Icon={_icon(target, db)}", f'Exec="{exe}" run --to {target} %F']
    return "\n".join(lines) + "\n"


def _tool_file(t: Tool, mimes: list[str], excludes: list[str], exe: str) -> str:
    """One "Hala tools" entry. Tools that take a selection together need two or more files."""
    lines = [
        "[Desktop Entry]", "Type=Service", f"X-Hala-Menu=tool-{t.name}",
        "X-KDE-ServiceTypes=KonqPopupMenu/Plugin", "X-KDE-Protocols=file",
        f"MimeType={';'.join(mimes)};", *translated("X-KDE-Submenu", "Hala tools"), "Icon=hala",
        "Actions=run;",
    ]
    if t.combine:
        lines.append(f"X-KDE-MinNumberOfUrls={t.min_files}")
    if excludes:
        lines.append(f"ExcludeServiceTypes={';'.join(excludes)};")
    dots = "…" if t.asks else ""
    lines += ["", "[Desktop Action run]", *translated("Name", t.label, suffix=dots),
              f"Icon={t.icon}",
              f'Exec="{exe}" run --tool {t.name} %F']
    return "\n".join(lines) + "\n"


def _edit_file(mimes: list[str], excludes: list[str], exe: str) -> str:
    """"Crop, annotate, redact…" in the Hala tools submenu, for pictures and PDFs."""
    lines = [
        "[Desktop Entry]", "Type=Service", "X-Hala-Menu=edit",
        "X-KDE-ServiceTypes=KonqPopupMenu/Plugin", "X-KDE-Protocols=file",
        f"MimeType={';'.join(mimes)};", *translated("X-KDE-Submenu", "Hala tools"), "Icon=hala",
        "Actions=edit;",
    ]
    if excludes:
        lines.append(f"ExcludeServiceTypes={';'.join(excludes)};")
    lines += ["", "[Desktop Action edit]", *translated("Name", "Crop, annotate, redact…"),
              "Icon=draw-freehand", f'Exec="{exe}" edit %F']
    return "\n".join(lines) + "\n"


def build_menus(exe: str, db: MimeDb | None = None) -> dict[str, str]:
    """File name -> contents for every menu file."""
    db = db or MimeDb()
    every_ext = sorted({e for r in ROUTES for e in r.sources if e not in (ANY, f.FOLDER)})
    mimes_of = {ext: db.mimes_for(ext) for ext in every_ext}
    every_type = db.all_types()

    def excludes_for(own: list[str]) -> list[str]:
        # Types that would match through inheritance but aren't this menu's: e.g. the txt
        # menu (text/plain) excludes text/csv (the csv menu's) and text/x-python (no menu).
        return sorted(m for m in every_type - set(own)
                      if any(db.inherits(m, o) for o in own))

    menus: dict[str, str] = {}
    for ext in every_ext:
        if not mimes_of[ext] or not (targets := _menu_targets([ext])):
            continue
        name = f"{PREFIX}{ext.replace('.', '_')}.desktop"
        menus[name] = _desktop_file(ext, mimes_of[ext], excludes_for(mimes_of[ext]), targets,
                                    db, exe, single=True)
    for family, exts in f.FAMILIES.items():
        mimes = sorted({m for e in exts for m in mimes_of.get(e, [])})
        if not mimes or not (targets := _menu_targets(sorted(exts))):
            continue
        menus[f"{PREFIX}family-{family}.desktop"] = _desktop_file(
            family, mimes, excludes_for(mimes), targets, db, exe, single=False)
    for t in tools().values():
        if t.hidden:
            continue
        mimes = sorted({m for e in t.sources for m in db.mimes_for(e)})
        if mimes:
            menus[f"{PREFIX}tool-{t.name}.desktop"] = _tool_file(t, mimes, excludes_for(mimes),
                                                                 exe)
    # Every picture we read, and PDFs (hala/editing.py imports Qt, so it isn't imported here).
    editable = sorted({m for e in (*f.IMAGE_IN, "pdf") for m in db.mimes_for(e)})
    if editable:
        menus[f"{PREFIX}edit.desktop"] = _edit_file(editable, excludes_for(editable), exe)
    menus[f"{PREFIX}shelf.desktop"] = localize(SHELF_MENU.format(exe=exe))
    menus[f"{PREFIX}wheel.desktop"] = localize(WHEEL_MENU.format(exe=exe))
    return menus


def translated(key: str, msgid: str, *, context: str | None = None, suffix: str = "",
               languages: Iterable[str] | None = None) -> list[str]:
    """"Key=English" and a "Key[lang]=..." line for each language that translates msgid
    (English text, never already translated: the menus are the same whoever builds them)."""
    lines = [f"{key}={msgid}{suffix}"]
    for lang in i18n.available() if languages is None else languages:
        catalog = i18n.translator(lang)
        text = catalog.gettext(msgid) if context is None else catalog.pgettext(context, msgid)
        if text != msgid:
            lines.append(f"{key}[{lang}]={text}{suffix}")
    return lines


def localize(text: str, languages: Iterable[str] | None = None) -> str:
    """A fixed desktop entry (APP_ENTRY...) with a Key[lang]= line after each translatable
    Key= line (Name, Comment...) whose whole value is marked text, as in DESKTOP_TEXT."""
    out: list[str] = []
    for line in text.splitlines():
        key, eq, value = line.partition("=")
        if eq and key in LOCALIZED_KEYS and value:
            out += translated(key, value, languages=languages)
        else:
            out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


# Top level of the right-click menu, for any files: the wheel without dragging to the screen
# edge. Without X-KDE-Priority=TopLevel, Dolphin files it under "Actions".
WHEEL_MENU = """[Desktop Entry]
Type=Service
X-Hala-Menu=wheel
X-KDE-ServiceTypes=KonqPopupMenu/Plugin
X-KDE-Protocols=file
X-KDE-Priority=TopLevel
MimeType=all/all;
Icon=hala
Actions=wheel;

[Desktop Action wheel]
Name=Hala…
Icon=hala
Exec="{exe}" wheel %F
"""


# Any file or folder, any number of them.
SHELF_MENU = """[Desktop Entry]
Type=Service
X-Hala-Menu=shelf
X-KDE-ServiceTypes=KonqPopupMenu/Plugin
X-KDE-Protocols=file
MimeType=all/all;
X-KDE-Submenu=Hala tools
Icon=hala
Actions=shelf;

[Desktop Action shelf]
Name=Put on shelf
Icon=view-list-icons
Exec="{exe}" action shelf %F
"""


# The application menu entry. It is also what notifications name as their app (desktop-entry
# "hala") and what "Open with" offers.
APP_ENTRY = """[Desktop Entry]
Type=Application
Name=Hala
GenericName=File Converter
Comment=Convert, compress and edit files locally: drag them to the screen edge
Icon=hala
Exec="{exe}" app %F
Categories=Utility;Qt;KDE;
Keywords=convert;converter;compress;pdf;image;video;audio;
X-KDE-StartupNotify=false
X-Hala-Menu=app
Actions=wheel;shelf;

[Desktop Action wheel]
Name=Open the wheel for copied files
Icon=hala
Exec="{exe}" wheel --clipboard

[Desktop Action shelf]
Name=Open the shelf
Icon=view-list-icons
Exec="{exe}" shelf
"""

# hala:// links (e.g. a tile on a web dashboard) start Hala like the app menu does. A link needs
# its own entry: with %F, KDE would try to download the link as a file first.
LINK_ENTRY = """[Desktop Entry]
Type=Application
Name=Hala
Icon=hala
Exec="{exe}" app %u
NoDisplay=true
MimeType=x-scheme-handler/hala;
X-Hala-Menu=link
"""

# KDE's global-shortcut service reads default shortcuts from files like this one; the user can
# change or clear it in System Settings > Keyboard > Shortcuts ("Hala wheel").
SHORTCUT_ENTRY = """[Desktop Entry]
Type=Application
Name=Hala wheel
Comment=Open the format wheel for the files last copied with Ctrl+C
Icon=hala
Exec="{exe}" wheel --clipboard
NoDisplay=true
X-KDE-Shortcuts={shortcut}
X-Hala-Menu=shortcut
"""


def exec_arg(path: str) -> str:
    """path escaped to sit inside the double quotes of an Exec= line (desktop entry spec):
    backslash, quote, backtick and dollar get a backslash - itself doubled, as Exec is a
    string value - and % is doubled so it isn't read as a field code."""
    quoted = "".join("\\" + ch if ch in '\\"`$' else ch for ch in path)
    return quoted.replace("\\", "\\\\").replace("%", "%%")


def install(exe: str | None = None) -> list[Path]:
    exe = exec_arg(exe or shutil.which("hala") or "hala")
    MENU_DIR.mkdir(parents=True, exist_ok=True)
    uninstall()
    written = []
    for name, text in build_menus(exe).items():
        path = MENU_DIR / name
        path.write_text(text)
        path.chmod(0o755)  # Plasma 6 ignores service menus that aren't executable
        written.append(path)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    (APP_DIR / "hala.desktop").write_text(localize(APP_ENTRY.format(exe=exe)))
    (APP_DIR / "hala-link.desktop").write_text(LINK_ENTRY.format(exe=exe))
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ICON, ICON_DIR / "hala.svg")
    SHORTCUT_DIR.mkdir(parents=True, exist_ok=True)
    (SHORTCUT_DIR / SHORTCUT_FILE).write_text(
        localize(SHORTCUT_ENTRY.format(exe=exe, shortcut=DEFAULT_SHORTCUT)))
    return written


def uninstall() -> int:
    """Remove the menus (and the per-user app entry) that install() wrote; the count is menus."""
    removed = 0
    for path in MENU_DIR.glob(f"{PREFIX}*.desktop"):
        if "X-Hala-Menu=" in path.read_text(errors="replace"):  # only our own files
            path.unlink()
            removed += 1
    for ours in (SHORTCUT_DIR / SHORTCUT_FILE, APP_DIR / "hala-link.desktop"):
        if ours.exists() and "X-Hala-Menu=" in ours.read_text(errors="replace"):
            ours.unlink()
    app = APP_DIR / "hala.desktop"
    try:
        text = app.read_text(errors="replace")
    except FileNotFoundError:
        return removed
    # Our own entry: marked since 0.1.0's packaging, or the older unmarked one.
    if "X-Hala-Menu=" in text or "GenericName=File Converter" in text:
        app.unlink()
    return removed


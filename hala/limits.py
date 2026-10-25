"""Limits that stop a hostile or accidental input from filling the disk or memory.

Zip bombs both ways: an archive may not unpack to far more than its own size (nor to more
entries than any real archive has, nor to more than the free space), and Hala won't make one
either: no sparse files in an archive (gigabytes on paper, nothing on disk), and an archive
it packed that would unpack to far more than its size is thrown away. Real archives of
ordinary files stay well inside these: text compresses 3-10 times, logs up to ~50.

Four of them can be raised (or lowered) for very large but legitimate inputs, in
~/.config/hala/limits.conf, which `hala limits` shows and changes. The file is read again
whenever it changes, so a running drop zone picks up a new value at its next job.
"""

from __future__ import annotations

import configparser
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .i18n import N_, _, ngettext
from .runner import ConversionError

# More than this many times the archive's size, once past BOMB_FLOOR, is a bomb's shape.
BOMB_RATIO = 100
BOMB_FLOOR = 1 << 30  # 1 GiB: below it, any ratio is harmless
MAX_ENTRIES = 100_000
# Pictures larger than this are refused (a 500-megapixel PNG of one colour is only a few
# hundred KB).
MAX_PIXELS = 500_000_000
# A job needs this much free space beside its output, or twice its input if that's larger.
MIN_FREE = 100 << 20
# A file is sparse when its blocks on disk cover less than this share of its size.
SPARSE_SHARE = 0.5
SPARSE_FLOOR = 64 << 20  # smaller sparse files (some databases, VM images) are left alone


# --- the limits people may change -----------------------------------------------------------

@dataclass(frozen=True)
class Setting:
    key: str  # the name in limits.conf and `hala limits set`
    attr: str  # the module constant it replaces
    scale: int  # the constant is the setting times this
    lowest: int
    highest: int  # keeps a typo (an extra few zeros) from switching a check off
    help: str  # English; shown through _()


SETTINGS = (
    Setting("max_megapixels", "MAX_PIXELS", 1_000_000, 1, 20_000,
            N_("pictures (and PDF pages, as drawn) larger than this are refused")),
    Setting("max_archive_entries", "MAX_ENTRIES", 1, 100, 10_000_000,
            N_("archives holding more files than this are refused")),
    Setting("max_unpack_ratio", "BOMB_RATIO", 1, 10, 100_000,
            N_("archives that unpack to more than this many times their size are "
               "refused...")),
    Setting("unpack_floor_gb", "BOMB_FLOOR", 1 << 30, 1, 10_000,
            N_("...once they unpack to more than this many GB")),
)
DEFAULTS = {s.key: globals()[s.attr] // s.scale for s in SETTINGS}


def config_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "hala" / "limits.conf"


def _setting(key: str) -> Setting:
    for s in SETTINGS:
        if s.key == key:
            return s
    raise KeyError(unknown_limit(key))


def unknown_limit(key: str) -> str:
    return _("no limit called {key!r}; there are {keys}").format(key=key,
                                                                 keys=", ".join(DEFAULTS))


def parse_value(key: str, text: str) -> int:
    """A setting's value from text, or ValueError saying what's allowed."""
    s = _setting(key)
    try:
        value = int(text.strip().replace("_", "").replace(",", ""))
    except ValueError:
        raise ValueError(_("{key} must be a whole number, not {text!r}").format(
            key=key, text=text.strip())) from None
    if not s.lowest <= value <= s.highest:
        raise ValueError(_("{key} must be between {lowest:,} and {highest:,}").format(
            key=key, lowest=s.lowest, highest=s.highest))
    return value


def read_config() -> tuple[dict[str, int], list[str]]:
    """The values limits.conf sets, and what's wrong with it.
    A bad or unknown line is skipped, never fatal: the default applies."""
    path = config_file()
    parser = configparser.ConfigParser(interpolation=None)  # a "%" is just a character
    try:
        parser.read_string(path.read_text(encoding="utf-8"), source=str(path))
    except FileNotFoundError:
        return {}, []
    except (OSError, UnicodeDecodeError, configparser.Error) as exc:
        return {}, [_("{path}: can't read it ({error}); using the defaults").format(
            path=path, error=exc)]
    found: dict[str, int] = {}
    problems = []
    if parser.sections() and "limits" not in parser:
        problems.append(_("{path}: no [limits] section; using the defaults").format(path=path))
    for key, text in (parser["limits"].items() if "limits" in parser else ()):
        try:
            found[key] = parse_value(key, text)
        except (KeyError, ValueError) as exc:
            problems.append(_("{path}: {problem}; ignored").format(path=path, problem=exc.args[0]))
    return found, problems


def write_config(values: dict[str, int]) -> Path:
    """Save the settings that differ from the defaults (none: the file is removed)."""
    path = config_file()
    changed = {k: v for k, v in values.items() if DEFAULTS.get(k) != v}
    if not changed:
        path.unlink(missing_ok=True)
        _loaded["stamp"] = "written"
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Hala's size limits (see `hala limits`). Delete a line to go back to its default.",
             "[limits]", *(f"{k} = {v}" for k, v in changed.items())]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    _loaded["stamp"] = "written"  # read again (and applied) at the next check
    return path


# The (inode, mtime, size) of the limits.conf last applied. Constants only change when the file
# does, so a test (or anything else) that sets one directly keeps it.
_loaded: dict[str, object] = {}


def refresh() -> None:
    """Apply limits.conf to the module's constants if it changed since it was last read."""
    try:
        st = config_file().stat()
        stamp: object = (st.st_ino, st.st_mtime_ns, st.st_size)  # saving makes a new inode
    except OSError:
        stamp = None
    if _loaded.get("stamp", "unread") == stamp:
        return
    values = read_config()[0] if stamp is not None else {}
    if stamp is None and "stamp" not in _loaded:
        _loaded["stamp"] = None  # no file and never was: leave the constants alone
        return
    for s in SETTINGS:
        globals()[s.attr] = values.get(s.key, DEFAULTS[s.key]) * s.scale
    _loaded["stamp"] = stamp


def current() -> dict[str, int]:
    refresh()
    return {s.key: globals()[s.attr] // s.scale for s in SETTINGS}


def raise_hint(key: str) -> str:
    return " " + _("(to allow it: hala limits set {key} N)").format(key=key)


def human(size: float) -> str:
    if size < 1000:
        return ngettext("{size} byte", "{size} bytes", int(size)).format(size=int(size))
    # Translators: file size units (kilobytes, megabytes...), e.g. "Ko", "Mo" in French.
    for unit in (_("KB"), _("MB"), _("GB")):
        size /= 1000
        if size < 1000:
            return f"{size:.1f} {unit}"
    terabytes = _("TB")
    return f"{size / 1000:.1f} {terabytes}"


def free_space(folder: Path) -> int:
    return shutil.disk_usage(folder).free


def check_free_space(folder: Path, inputs_size: int) -> None:
    """Refuse to start when the output's disk is nearly full."""
    need = max(MIN_FREE, 2 * inputs_size)
    try:
        free = free_space(folder)
    except OSError:
        return  # can't tell (some network mounts): let the tool report a real failure
    if free < need:
        raise ConversionError(_("not enough free space in {folder} ({free} free, {need} needed)")
                              .format(folder=folder, free=human(free), need=human(need)))


def free_or_none(folder: Path) -> int | None:
    """Free space in folder, or None when it can't be told (some network mounts)."""
    try:
        return free_space(folder)
    except OSError:
        return None


def unpack_cap(archive_size: int, free: int | None) -> int:
    """The most an archive of archive_size may unpack to: a bomb's size, or what fits."""
    refresh()
    cap = max(BOMB_FLOOR, BOMB_RATIO * max(archive_size, 1))
    return cap if free is None else max(0, min(cap, free - MIN_FREE))


def check_unpacked(total: int, entries: int, archive_size: int, free: int | None) -> None:
    """Refuse an archive whose contents are a bomb's shape or don't fit on the disk.

    free is the space there was before unpacking began (read once by the caller, so a
    running total isn't compared with space it has already used up); None skips that check.
    Runs for every member of an archive, so it doesn't re-read limits.conf: the caller does.
    """
    if entries > MAX_ENTRIES:
        raise ConversionError(_("the archive holds {entries:,} entries (more than {limit:,}); "
                                "refusing it as a likely zip bomb")
                              .format(entries=entries, limit=MAX_ENTRIES)
                              + raise_hint("max_archive_entries"))
    if total > BOMB_FLOOR and total > BOMB_RATIO * max(archive_size, 1):
        raise ConversionError(_("the archive would unpack to {total}, {ratio:,} times its size; "
                                "refusing it as a likely zip bomb")
                              .format(total=human(total), ratio=total // max(archive_size, 1))
                              + raise_hint("max_unpack_ratio"))
    if free is not None and total + MIN_FREE > free:
        raise ConversionError(_("the archive unpacks to {total}, but only {free} is free")
                              .format(total=human(total), free=human(free)))


def check_packed(total: int, archive: Path) -> None:
    """Refuse to keep an archive Hala made that unpacks to a bomb's ratio."""
    refresh()
    size = archive.stat().st_size
    if total > BOMB_FLOOR and total > BOMB_RATIO * max(size, 1):
        raise ConversionError(_("the archive would unpack to {total}, {ratio:,} times its size: "
                                "that's the shape of a zip bomb, so it wasn't made")
                              .format(total=human(total), ratio=total // max(size, 1))
                              + raise_hint("max_unpack_ratio"))


def check_not_sparse(path: Path) -> None:
    """Refuse to pack a sparse file: it unpacks to its full size, not to what it uses."""
    try:
        st = path.lstat()
    except OSError:
        return
    if not path.is_file() or path.is_symlink() or st.st_size < SPARSE_FLOOR:
        return
    on_disk = st.st_blocks * 512
    if on_disk < st.st_size * SPARSE_SHARE:
        raise ConversionError(_("{name} is a sparse file ({size} in size but {on_disk} on disk); "
                                "packing it would make an archive that unpacks far larger than "
                                "it looks, so the archive wasn't made (move that file out of "
                                "the selection)")
                              .format(name=path.name, size=human(st.st_size),
                                      on_disk=human(on_disk)))


def walk_files(root: Path) -> list[Path]:
    """Every file under root (root itself if it's a file), not following links."""
    if not root.is_dir() or root.is_symlink():
        return [root]
    found = []
    for folder, _dirs, files in os.walk(root):
        found += [Path(folder) / name for name in files]
    return found


def check_pixels(width: int, height: int, name: str) -> None:
    refresh()
    if width * height > MAX_PIXELS:
        raise ConversionError(_("{name} is {width:,} x {height:,} pixels ({megapixels:,.0f} "
                                "megapixels); pictures over {limit:,.0f} megapixels are refused")
                              .format(name=name, width=width, height=height,
                                      megapixels=width * height / 1e6,
                                      limit=MAX_PIXELS / 1e6)
                              + raise_hint("max_megapixels"))

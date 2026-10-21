"""Limits that stop a hostile or accidental input from filling the disk or memory.

Zip bombs both ways: an archive may not unpack to far more than its own size (nor to more
entries than any real archive has, nor to more than the free space), and Hala won't make one
either: no sparse files in an archive (gigabytes on paper, nothing on disk), and an archive
it packed that would unpack to far more than its size is thrown away. Real archives of
ordinary files stay well inside these: text compresses 3-10 times, logs up to ~50.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

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


def human(size: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"


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
        raise ConversionError(f"not enough free space in {folder} ({human(free)} free, "
                              f"{human(need)} needed)")


def free_or_none(folder: Path) -> int | None:
    """Free space in folder, or None when it can't be told (some network mounts)."""
    try:
        return free_space(folder)
    except OSError:
        return None


def unpack_cap(archive_size: int, free: int | None) -> int:
    """The most an archive of archive_size may unpack to: a bomb's size, or what fits."""
    cap = max(BOMB_FLOOR, BOMB_RATIO * max(archive_size, 1))
    return cap if free is None else max(0, min(cap, free - MIN_FREE))


def check_unpacked(total: int, entries: int, archive_size: int, free: int | None) -> None:
    """Refuse an archive whose contents are a bomb's shape or don't fit on the disk.

    free is the space there was before unpacking began (read once by the caller, so a
    running total isn't compared with space it has already used up); None skips that check.
    """
    if entries > MAX_ENTRIES:
        raise ConversionError(f"the archive holds {entries:,} entries (more than "
                              f"{MAX_ENTRIES:,}); refusing it as a likely zip bomb")
    if total > BOMB_FLOOR and total > BOMB_RATIO * max(archive_size, 1):
        raise ConversionError(f"the archive would unpack to {human(total)}, "
                              f"{total // max(archive_size, 1):,} times its size; "
                              "refusing it as a likely zip bomb")
    if free is not None and total + MIN_FREE > free:
        raise ConversionError(f"the archive unpacks to {human(total)}, but only {human(free)} "
                              "is free")


def check_packed(total: int, archive: Path) -> None:
    """Refuse to keep an archive Hala made that unpacks to a bomb's ratio."""
    size = archive.stat().st_size
    if total > BOMB_FLOOR and total > BOMB_RATIO * max(size, 1):
        raise ConversionError(f"the archive would unpack to {human(total)}, "
                              f"{total // max(size, 1):,} times its size: that's the shape "
                              "of a zip bomb, so it wasn't made")


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
        raise ConversionError(f"{path.name} is a sparse file ({human(st.st_size)} in size but "
                              f"{human(on_disk)} on disk); packing it would make an archive "
                              "that unpacks far larger than it looks, so the archive wasn't made "
                              "(move that file out of the selection)")


def walk_files(root: Path) -> list[Path]:
    """Every file under root (root itself if it's a file), not following links."""
    if not root.is_dir() or root.is_symlink():
        return [root]
    found = []
    for folder, _dirs, files in os.walk(root):
        found += [Path(folder) / name for name in files]
    return found


def check_pixels(width: int, height: int, name: str) -> None:
    if width * height > MAX_PIXELS:
        raise ConversionError(f"{name} is {width:,} x {height:,} pixels "
                              f"({width * height / 1e6:,.0f} megapixels); pictures over "
                              f"{MAX_PIXELS / 1e6:,.0f} megapixels are refused")

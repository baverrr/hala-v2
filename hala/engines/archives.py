"""Extracting and creating archives.

7-Zip for zip/7z/rar, Python's tarfile for tar/tar.gz/tar.bz2/tar.xz, system tar for tar.zst,
and gzip for a bare .gz.
"""

from __future__ import annotations

import gzip
import tarfile
import zlib
from collections.abc import Iterator
from pathlib import Path

from .. import limits
from ..formats import ARCHIVE_IN, ext_of, stem_of
from ..runner import Context, ConversionError

TARS = frozenset({"tar", "tar.gz", "tar.bz2", "tar.xz"})
# -spd: no wildcards or @listfiles in names ("report?.pdf" must not pull in its neighbours).
# -bso0 -bsp0: silence 7z's own stdout and progress output.
QUIET_7Z = ["-spd", "-bso0", "-bsp0"]
CHUNK = 1 << 20


def extract(ctx: Context, src: Path, dst_ext: str) -> Path:
    """Unpack into a folder. A single top-level entry is returned as-is (no double nesting)."""
    dest = ctx.workdir / "extracted" / stem_of(src)
    dest.mkdir(parents=True)
    src_ext = ext_of(src)
    size = src.stat().st_size
    if src_ext in TARS:
        try:
            with tarfile.open(src, "r:*") as tar:
                # "data" rejects absolute paths, "..", devices and links leaving dest.
                tar.extractall(dest, members=_checking(ctx, tar, size, dest), filter="data")
        except tarfile.FilterError as exc:
            raise ConversionError(f"unsafe entry in archive: {exc}") from None
        except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
            raise ConversionError(f"can't read this archive: {exc}") from None
    elif src_ext == "tar.zst":
        listing = ctx.run(["tar", "--zstd", "-tvf", src])
        sizes = [int(f[2]) for line in listing.splitlines()
                 if len(f := line.split()) > 2 and f[2].isdigit()]
        limits.check_unpacked(sum(sizes), len(sizes), size, dest)
        ctx.run(["tar", "--zstd", "-xf", src, "-C", dest, "--no-same-owner"])
        _check_links(dest)
    elif src_ext == "gz":
        try:
            written = 0
            with gzip.open(src, "rb") as fin, open(dest / stem_of(src), "wb") as fout:
                while chunk := fin.read(CHUNK):
                    ctx.check_cancelled()
                    written += len(chunk)
                    limits.check_unpacked(written, 1, size, dest)
                    fout.write(chunk)
        except (OSError, EOFError, zlib.error) as exc:
            raise ConversionError(f"can't read this archive: {exc}") from None
    else:
        total, entries = _listed_7z(ctx, src)
        limits.check_unpacked(total, entries, size, dest)
        ctx.run(["7z", "x", *QUIET_7Z, "-y", f"-o{dest}", src])
        _check_links(dest)
    entries = list(dest.iterdir())
    if not entries:
        raise ConversionError("the archive is empty")
    return entries[0] if len(entries) == 1 else dest


def pack(ctx: Context, src: Path, dst_ext: str) -> Path:
    """Compress a file or folder. An archive given as input is re-packed in the new format.
    Refuses sparse files, and an archive that would unpack to a zip bomb's ratio."""
    content = extract(ctx, src, "folder") if ext_of(src) in ARCHIVE_IN else src
    files = limits.walk_files(content)
    for path in files:
        ctx.check_cancelled()
        limits.check_not_sparse(path)
    total = sum(p.lstat().st_size for p in files if not p.is_symlink())
    ctx.progress(0.3)
    out = ctx.workdir / f"out.{dst_ext}"
    if dst_ext == "tar.gz":
        def cancellable(info: tarfile.TarInfo) -> tarfile.TarInfo:
            ctx.check_cancelled()
            return info

        with tarfile.open(out, "w:gz") as tar:
            tar.add(content, arcname=content.name, filter=cancellable)
    else:
        kind = {"zip": "-tzip", "7z": "-t7z"}[dst_ext]
        # Run from the parent so the archive holds "name/..." rather than an absolute path;
        # "./" keeps a name like "-rf" from being read as a switch.
        ctx.run(["7z", "a", *QUIET_7Z, kind, "-mx=7", out, f"./{content.name}"],
                cwd=content.parent)
    limits.check_packed(total, out)
    return out


def _checking(ctx: Context, tar: tarfile.TarFile, size: int,
              dest: Path) -> Iterator[tarfile.TarInfo]:
    """The members, each checked (cancel, zip-bomb limits) before it's written."""
    total = 0
    for count, member in enumerate(tar, 1):
        ctx.check_cancelled()
        total += member.size if member.isfile() else 0
        limits.check_unpacked(total, count, size, dest)
        yield member


def _listed_7z(ctx: Context, src: Path) -> tuple[int, int]:
    """(total unpacked size, entries) from 7z's technical listing, before anything is written."""
    listing = ctx.run(["7z", "l", "-slt", "-ba", *QUIET_7Z[:1], src])
    sizes = [int(line[7:]) for line in listing.splitlines()
             if line.startswith("Size = ") and line[7:].strip().isdigit()]
    entries = sum(1 for line in listing.splitlines() if line.startswith("Path = "))
    return sum(sizes), entries


def _check_links(dest: Path) -> None:
    """Refuse archives whose symlinks point outside the extraction folder."""
    root = dest.resolve()
    for path in dest.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(root):
            raise ConversionError(f"unsafe link in archive: {path.relative_to(dest)}")

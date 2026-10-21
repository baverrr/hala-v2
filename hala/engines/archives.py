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
# What 7z must find inside each extension. 7z picks its decoder by content, so a bzip2 or
# gzip stream named .zip would otherwise unpack with no sizes listed (or forged ones).
TYPES_7Z = {"zip": {"zip"}, "7z": {"7z"}, "rar": {"rar", "rar5"}}


def extract(ctx: Context, src: Path, dst_ext: str) -> Path:
    """Unpack into a folder. A single top-level entry is returned as-is (no double nesting)."""
    dest = ctx.workdir / "extracted" / stem_of(src)
    dest.mkdir(parents=True)
    src_ext = ext_of(src)
    size = src.stat().st_size
    # Read once: the running totals below are compared with the space there was at the start.
    free = limits.free_or_none(dest)
    if src_ext in TARS:
        try:
            with tarfile.open(src, "r:*") as tar:
                # "data" rejects absolute paths, "..", devices and links leaving dest.
                tar.extractall(dest, members=_checking(ctx, tar, size, free), filter="data")
        except tarfile.FilterError as exc:
            raise ConversionError(f"unsafe entry in archive: {exc}") from None
        except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
            raise ConversionError(f"can't read this archive: {exc}") from None
    elif src_ext == "tar.zst":
        listing = ctx.run(["tar", "--zstd", "-tvf", src])
        sizes = [int(f[2]) for line in listing.splitlines()
                 if len(f := line.split()) > 2 and f[2].isdigit()]
        limits.check_unpacked(sum(sizes), len(sizes), size, free)
        _capped(ctx, ["tar", "--zstd", "-xf", src, "-C", dest, "--no-same-owner"],
                dest, size, free)
        _check_links(dest)
    elif src_ext == "gz":
        try:
            written = 0
            with gzip.open(src, "rb") as fin, open(dest / stem_of(src), "wb") as fout:
                while chunk := fin.read(CHUNK):
                    ctx.check_cancelled()
                    written += len(chunk)
                    limits.check_unpacked(written, 1, size, free)
                    fout.write(chunk)
        except (OSError, EOFError, zlib.error) as exc:
            raise ConversionError(f"can't read this archive: {exc}") from None
    else:
        kind, total, entries = _listed_7z(ctx, src, src_ext)
        limits.check_unpacked(total, entries, size, free)
        # -t: unpack with the decoder the listing was checked with, not whatever 7z guesses.
        _capped(ctx, ["7z", "x", *QUIET_7Z, f"-t{kind}", "-y", f"-o{dest}", src],
                dest, size, free)
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
              free: int | None) -> Iterator[tarfile.TarInfo]:
    """The members, each checked (cancel, zip-bomb limits) before it's written."""
    total = 0
    for count, member in enumerate(tar, 1):
        ctx.check_cancelled()
        total += member.size if member.isfile() else 0
        limits.check_unpacked(total, count, size, free)
        yield member


def _capped(ctx: Context, cmd: list[str | Path], dest: Path, size: int,
            free: int | None) -> None:
    """Run an unpacker that may not write any file past the archive's cap (RLIMIT_FSIZE),
    then check the total. A backstop in case its listing understated the contents."""
    cap = limits.unpack_cap(size, free)
    try:
        ctx.run(["prlimit", f"--fsize={cap}", "--", *cmd])
    except ConversionError:
        if _unpacked_size(dest) >= cap:
            raise _too_big(cap) from None
        raise
    if _unpacked_size(dest) > cap:
        raise _too_big(cap)


def _too_big(cap: int) -> ConversionError:
    return ConversionError(f"the archive would unpack to more than {limits.human(cap)}, "
                           "more than its listing said; refusing it as a likely zip bomb")


def _unpacked_size(dest: Path) -> int:
    return sum(p.lstat().st_size for p in limits.walk_files(dest)
               if p.is_file() and not p.is_symlink())


def _listed_7z(ctx: Context, src: Path, ext: str) -> tuple[str, int, int]:
    """(archive type, total unpacked size, entries) from 7z's technical listing, before
    anything is written. Refuses a listing that can't be trusted: the wrong archive type
    for the extension, no entries, or an entry without a size."""
    listing = ctx.run(["7z", "l", "-slt", *QUIET_7Z[:1], src])
    head, sep, body = listing.partition("\n----------\n")
    kinds = [line[7:].strip().lower() for line in head.splitlines()
             if line.startswith("Type = ")]
    kind = kinds[-1] if kinds else ""
    if not sep or kind not in TYPES_7Z.get(ext, {ext}):
        found = f"a {kind} file" if kind else "not a readable archive"
        raise ConversionError(f"{src.name} is {found}, not a .{ext} archive; refusing it")
    total = entries = 0
    for block in body.split("\n\n"):
        fields = dict(line.split(" = ", 1) for line in block.splitlines() if " = " in line)
        if "Path" not in fields:
            continue
        entries += 1
        if not fields.get("Size", "").strip().isdigit():
            raise ConversionError(f"{src.name} doesn't list the size of "
                                  f"{fields['Path']!r}; refusing it as a likely zip bomb")
        total += int(fields["Size"])
    if not entries:
        raise ConversionError("the archive is empty")
    return kind, total, entries


def _check_links(dest: Path) -> None:
    """Refuse archives whose symlinks point outside the extraction folder."""
    root = dest.resolve()
    for path in dest.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(root):
            raise ConversionError(f"unsafe link in archive: {path.relative_to(dest)}")

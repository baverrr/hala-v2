"""Converting one file: pick the route, work in a scratch dir, then move the result into place.

produce() is the scratch-dir-and-placement part on its own; the tools use it too.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import formats as f
from .registry import route_for, targets_for
from .runner import Context, ConversionError

SCRATCH_PREFIX = ".hala-"
# Scratch dirs this old were left by a hala that was killed outright; nothing still uses them.
STALE_AFTER_S = 24 * 3600


def convert_file(src: Path, dst_ext: str, *, outdir: Path | None = None,
                 on_progress: Callable[[float], None] | None = None,
                 cancel_event: threading.Event | None = None) -> Path:
    """Convert src to dst_ext next to it (or into outdir). Never overwrites; returns the result."""
    src = src.expanduser().resolve()
    if not src.exists():
        raise ConversionError(f"{src} does not exist")
    dst_ext = f.normalize_ext(dst_ext)
    src_ext = f.ext_of(src)
    route = route_for(src_ext, dst_ext)
    if route is None:
        offered = ", ".join(targets_for(src_ext)) or "nothing"
        raise ConversionError(f"can't convert {src_ext or 'this file'} to {dst_ext} "
                              f"(it can become: {offered})")
    return produce(src, lambda ctx: route.engine(ctx, src, dst_ext),
                   lambda produced: _final_name(src, dst_ext, produced),
                   outdir=outdir, on_progress=on_progress, cancel_event=cancel_event)


def produce(src: Path, work: Callable[[Context], Path], name: Callable[[Path], str], *,
            outdir: Path | None = None, on_progress: Callable[[float], None] | None = None,
            cancel_event: threading.Event | None = None) -> Path:
    """Run work in a scratch dir beside src (or in outdir), then move what it made into place
    under name(produced), never overwriting anything. Shared by conversions and tools."""
    parent = (outdir.expanduser().resolve() if outdir else src.parent)
    try:
        parent.mkdir(parents=True, exist_ok=True)
        # Scratch space on the same filesystem, so the final move is a rename, not a copy.
        workdir = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=parent))
    except OSError as exc:
        raise ConversionError(f"can't write to {parent}: {exc.strerror} "
                              "(pick another folder with -o)") from None
    _sweep_stale(parent)
    ctx = Context(workdir, on_progress=on_progress or (lambda _f: None),
                  cancel_event=cancel_event or threading.Event())
    try:
        produced = work(ctx)
        ctx.check_cancelled()  # a cancel during a pure-Python step must not end up "done"
        final = _place(produced, parent, name(produced))
        ctx.progress(1.0)
        return final
    finally:
        _remove_tree(workdir)


def _final_name(src: Path, dst_ext: str, produced: Path) -> str:
    stem = f.stem_of(src)
    if dst_ext == f.FOLDER:
        # The archive's single top-level entry, or a folder named after the archive.
        return produced.name
    if produced.is_dir():
        return f"{stem} ({dst_ext.upper()} pages)"
    return f"{stem}.{dst_ext}"


def _place(produced: Path, parent: Path, name: str) -> Path:
    """Move produced into parent as name, or "name (2)", "name (3)"… if taken.

    The name is claimed atomically before anything moves (a hard link, an empty placeholder
    folder, or an O_EXCL placeholder file), so two jobs finishing at once can't collide.
    """
    is_dir = produced.is_dir() and not produced.is_symlink()  # a link is placed as itself
    stem, ext = (name, "") if is_dir else f.split_name(name)
    try:
        for n in range(1, 1000):
            candidate = parent / (f"{stem}{ext}" if n == 1 else f"{stem} ({n}){ext}")
            if is_dir:
                try:
                    candidate.mkdir()
                except FileExistsError:
                    continue
                try:
                    os.rename(produced, candidate)  # replaces our own empty placeholder
                except OSError:
                    candidate.rmdir()
                    raise
                return candidate
            try:
                # Fails instead of clobbering, unlike rename; links the symlink, not its target.
                os.link(produced, candidate, follow_symlinks=False)
            except FileExistsError:
                continue
            except OSError:
                # No hard links here (FAT/exFAT and some FUSE mounts): claim with O_EXCL.
                try:
                    # Private: os.replace below swaps in the result, which keeps its own mode.
                    os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                except FileExistsError:
                    continue
                try:
                    os.replace(produced, candidate)
                except OSError:
                    candidate.unlink(missing_ok=True)
                    raise
                return candidate
            produced.unlink()
            return candidate
    except OSError as exc:
        raise ConversionError(f"can't save the result in {parent}: {exc.strerror}") from None
    raise ConversionError(f"too many existing copies of {name} in {parent}")


def _remove_tree(path: Path) -> None:
    """rmtree that also clears read-only folders unpacked from archives (mode 0555)."""

    def make_writable(func: Callable[..., object], p: str, _exc: BaseException) -> None:
        os.chmod(os.path.dirname(p), stat.S_IRWXU)
        func(p)

    try:
        shutil.rmtree(path, onexc=make_writable)
    except OSError:
        shutil.rmtree(path, ignore_errors=True)


def _sweep_stale(parent: Path) -> None:
    cutoff = time.time() - STALE_AFTER_S
    for old in parent.glob(SCRATCH_PREFIX + "*"):
        try:
            if old.is_dir() and old.stat().st_mtime < cutoff:
                _remove_tree(old)
        except OSError:
            pass  # someone else's, or already gone

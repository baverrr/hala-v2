"""Actions: things to do with files rather than to them - the bubbles under the wheel.

Zip runs as a (hidden) tool, so it gets Plasma progress and a result notification like any
tool. Copy and move never overwrite: a taken name becomes "name (2)". Symlinks are copied and
moved as links, never as what they point to. A move within one filesystem is a rename (or a
hard link plus unlink) under a freshly claimed name; otherwise - including between bind mounts
of one disk - the copy is finished and placed before the original is deleted, and a move that
gets that far but can't remove the original says so (PartialMove) rather than "failed".
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import formats as f
from .convert import produce
from .runner import Context, ConversionError

KDECONNECT = "kdeconnect-cli"


class PartialMove(ConversionError):
    """The file arrived at `placed`, but the original couldn't be (fully) removed."""

    def __init__(self, message: str, placed: Path):
        super().__init__(message)
        self.placed = placed


@dataclass(frozen=True)
class Action:
    name: str  # "zip"; the wheel's target is "action:zip"
    label: str
    icon: str
    files_only: bool = False  # KDE Connect can't share folders
    # Zip only for several items: one item already has the wheel's ZIP format petal.
    min_count: int = 1

    def accepts(self, paths: Sequence[Path]) -> bool:
        return (len(paths) >= self.min_count
                and (not self.files_only or all(p.is_file() for p in paths)))


ACTIONS: dict[str, Action] = {a.name: a for a in (
    Action("shelf", "Shelf", "view-list-icons"),
    Action("phone", "Phone", "kdeconnect", files_only=True),
    Action("copy", "Copy to", "edit-copy"),
    Action("move", "Move to", "go-jump"),
    Action("zip", "Zip all", "application-zip", min_count=2),
)}


def actions_for(paths: Sequence[Path]) -> list[str]:
    return [a.name for a in ACTIONS.values() if a.accepts(paths)]


# --- copy / move ----------------------------------------------------------------------------


def _is_folder(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _why(exc: OSError) -> str:
    if isinstance(exc, shutil.Error) and exc.args and isinstance(exc.args[0], list):
        problems = exc.args[0]  # copytree: [(src, dst, reason), ...]
        first = problems[0][2] if problems and len(problems[0]) == 3 else str(exc)
        more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
        return f"{first}{more}"
    return exc.strerror or str(exc)


def copy_to(src: Path, folder: Path) -> Path:
    """Copy a file, folder or link into folder, never overwriting; returns where it went."""
    if not os.path.lexists(src):
        raise ConversionError(f"{src.name} no longer exists")
    _refuse_into_itself(src, folder)

    def work(ctx: Context) -> Path:
        dest = ctx.workdir / src.name
        if src.is_symlink():
            os.symlink(os.readlink(src), dest)
        elif src.is_dir():
            shutil.copytree(src, dest, symlinks=True)
        else:
            shutil.copy2(src, dest)
        return dest

    try:
        return produce(src, work, lambda produced: produced.name, outdir=folder)
    except OSError as exc:  # copying (disk full, unreadable file); placement reports its own
        raise ConversionError(f"couldn't copy {src.name}: {_why(exc)}") from None


def move_to(src: Path, folder: Path) -> Path:
    """Move a file, folder or link into folder, never overwriting; returns where it went."""
    if not os.path.lexists(src):
        raise ConversionError(f"{src.name} no longer exists")
    folder = folder.expanduser().resolve()
    if src.parent.resolve() == folder:
        return src  # already there
    _refuse_into_itself(src, folder)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        same_disk = src.lstat().st_dev == folder.stat().st_dev
    except OSError as exc:
        raise ConversionError(f"can't move into {folder}: {_why(exc)}") from None
    if same_disk:
        try:
            return _rename_into(src, folder)
        except OSError as exc:
            if exc.errno != errno.EXDEV:  # bind mounts: same disk, but no renames across
                raise ConversionError(f"couldn't move {src.name}: {_why(exc)}") from None
    placed = copy_to(src, folder)
    try:
        if _is_folder(src):
            shutil.rmtree(src)
        else:
            src.unlink()
    except OSError as exc:
        raise PartialMove(f"copied to {folder.name}, but the original couldn't be removed "
                          f"({_why(exc)}){' - it may be partly deleted' * _is_folder(src)}",
                          placed) from None
    return placed


def _free_names(folder: Path, name: str, is_dir: bool) -> Iterator[Path]:
    stem, ext = (name, "") if is_dir else f.split_name(name)
    for n in range(1, 1000):
        yield folder / (f"{stem}{ext}" if n == 1 else f"{stem} ({n}){ext}")
    raise ConversionError(f"too many existing copies of {name} in {folder}")


def _rename_into(src: Path, folder: Path) -> Path:
    """Move within one filesystem under a free name. The name is claimed first (a hard link,
    or an empty placeholder the rename replaces), so nothing is ever overwritten. Raises
    OSError (EXDEV between bind mounts: the caller copies instead)."""
    if _is_folder(src):
        for candidate in _free_names(folder, src.name, is_dir=True):
            try:
                candidate.mkdir()
            except FileExistsError:
                continue
            try:
                os.rename(src, candidate)  # replaces our own empty placeholder
            except OSError:
                candidate.rmdir()
                raise
            return candidate
    for candidate in _free_names(folder, src.name, is_dir=False):
        try:
            os.link(src, candidate, follow_symlinks=False)
        except FileExistsError:
            continue
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise
            # No hard links here (FAT/exFAT, some FUSE): claim with O_EXCL, rename over it.
            try:
                os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            except FileExistsError:
                continue
            try:
                os.rename(src, candidate)
            except OSError:
                candidate.unlink(missing_ok=True)
                raise
            return candidate
        try:
            src.unlink()
        except OSError as exc:
            raise PartialMove(f"linked into {folder.name}, but couldn't remove the original "
                              f"from {src.parent} ({_why(exc)})", candidate) from None
        return candidate
    raise AssertionError("unreachable")  # _free_names raises when it runs out


def _refuse_into_itself(src: Path, folder: Path) -> None:
    if _is_folder(src) and folder.resolve().is_relative_to(src.resolve()):
        raise ConversionError(f"can't put {src.name} inside itself")


# --- KDE Connect ----------------------------------------------------------------------------


def _kdeconnect(*args: str, timeout: float) -> str:
    if shutil.which(KDECONNECT) is None:
        raise ConversionError("KDE Connect isn't installed (apt install kdeconnect)")
    try:
        return subprocess.run([KDECONNECT, *args], capture_output=True, text=True,
                              timeout=timeout, check=True).stdout
    except subprocess.TimeoutExpired:
        raise ConversionError(f"KDE Connect didn't respond in {timeout:.0f} s "
                              "(is kdeconnectd running?)") from None
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()
        raise ConversionError(detail[-1] if detail else f"exit code {exc.returncode}") from None
    except OSError as exc:
        raise ConversionError(f"couldn't run {KDECONNECT}: {_why(exc)}") from None


def phones() -> list[tuple[str, str]]:
    """(id, name) of paired devices that are reachable right now."""
    out = _kdeconnect("--list-available", "--id-name-only", timeout=10)
    found = []
    for line in out.splitlines():
        device_id, _, name = line.strip().partition(" ")
        if device_id:
            found.append((device_id, name or device_id))
    return found


def send_to_phone(files: Sequence[Path], device_id: str) -> list[tuple[Path, str]]:
    """Hand each file to KDE Connect, which then shows its own transfer progress. Returns
    (file, reason) for the ones it refused; the rest are queued, not necessarily arrived."""
    refused = []
    for path in files:
        if not path.is_file():
            refused.append((path, "only files can be sent, not folders"))
            continue
        try:
            _kdeconnect("--device", device_id, "--share", os.fspath(path), timeout=30)
        except ConversionError as exc:
            refused.append((path, str(exc)))
    return refused

"""Zip: every dropped file and folder into one .zip, beside the first (the wheel's Zip bubble).

Written with zipfile rather than 7z: 7z reads "@name" as a list file even after "--", and
flattens "./sub/name" to "name". zipfile takes any name, writes Zip64 when needed, and lets a
cancel land between files.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

from .. import formats as f
from ..registry import ANY
from ..runner import Context
from . import Params, Tool


def tidy(inputs: list[Path]) -> list[Path]:
    """Drop repeats and anything inside another selected folder (it's packed with it), so the
    archive - and its scratch folder - never end up inside something being zipped."""
    kept: list[Path] = []
    for path in dict.fromkeys(inputs):
        if not any(path != other and other.is_dir() and not other.is_symlink()
                   and path.is_relative_to(other) for other in inputs):
            kept.append(path)
    return kept


def _entries(path: Path, base: Path) -> list[tuple[Path, str]]:
    """(file on disk, name in the archive) for path and, for a folder, all inside it."""
    if not path.is_dir() or path.is_symlink():
        return [(path, path.relative_to(base).as_posix())]
    found = [(path, path.relative_to(base).as_posix() + "/")]
    for root, dirs, files in os.walk(path):  # followlinks=False: no loops through links
        dirs.sort()
        here = Path(root)
        for name in sorted(files):
            found.append((here / name, (here / name).relative_to(base).as_posix()))
        for name in dirs:  # a link to a folder is left out: its contents live elsewhere
            if not (here / name).is_symlink():
                found.append((here / name, (here / name).relative_to(base).as_posix() + "/"))
    return found


def pack(ctx: Context, inputs: list[Path], _params: Params) -> Path:
    # Names relative to the deepest common folder: "a.jpg", "sub/b.jpg", not absolute paths.
    base = Path(os.path.commonpath([p.parent for p in inputs]))
    entries = [e for p in inputs for e in _entries(p, base)]
    out = ctx.workdir / "out.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for i, (path, name) in enumerate(entries, 1):
            ctx.check_cancelled()
            if name.endswith("/"):
                archive.writestr(zipfile.ZipInfo.from_file(path, name), b"")
            else:
                archive.write(path, name)
            ctx.progress(i / len(entries))
    return out


def _name(inputs: list[Path]) -> str:
    return f.stem_of(inputs[0]) if len(inputs) == 1 else "Archive"


TOOL = Tool(
    "zip", "Zip", done="Zipped", sources=frozenset({ANY}), engine=pack, combine=True,
    suffix=lambda p: "zip", icon="application-zip", hidden=True, output_name=_name,
    tidy=tidy, help="pack the files and folders into one .zip beside them",
)

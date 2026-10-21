"""PDF merge (PDFs and images, in the order given), split (every page, or ranges) and unlock
(remove a password or restrictions)."""

from __future__ import annotations

import re
from pathlib import Path

from .. import formats as f
from ..engines import images, pdf
from ..runner import Context, ConversionError
from . import Param, Params, Tool

MERGE_SOURCES = frozenset({"pdf", *f.IMAGE_IN})


def merge(ctx: Context, inputs: list[Path], params: Params) -> Path:
    parts: list[Path] = []
    for i, src in enumerate(inputs):
        ctx.check_cancelled()
        if f.ext_of(src) == "pdf":
            pdf.require_unlocked(src)
            parts.append(src)
        else:
            page = images.images_to_pdf(ctx, [src])  # always workdir/out.pdf: move it aside
            parts.append(page.rename(ctx.workdir / f"image{i}.pdf"))
        ctx.progress(0.5 * (i + 1) / len(inputs))
    out = ctx.workdir / "merged.pdf"
    qpdf(ctx, ["--empty", "--pages", *parts, "--", out], out)
    return out


def qpdf(ctx: Context, args: list[str | Path], out: Path) -> None:
    """qpdf writing out. Exit code 3 means it worked but warned, usually about a damaged file
    it repaired: that's a result, with a note."""
    _stdout, _stderr, code = ctx.run_full(["qpdf", *args], ok_codes=(0, 3))
    if not out.exists():
        raise ConversionError("qpdf didn't write a PDF")
    if code == 3:
        repaired_note(ctx)


def repaired_note(ctx: Context) -> None:
    text = "a PDF was damaged; qpdf repaired it, so check the result"
    if text not in ctx.notes:
        ctx.note(text)


def page_count(ctx: Context, src: Path) -> int:
    return int(ctx.run_full(["qpdf", "--show-npages", src], ok_codes=(0, 3))[0].strip())


def parse_ranges(text: str, pages: int) -> list[tuple[int, int]]:
    """[(first, last), …] from "1-3, 5, 8-" (open end = last page); 1-based, inclusive."""
    ranges = []
    for part in filter(None, (p.strip() for p in text.split(","))):
        m = re.fullmatch(r"(\d+)\s*(?:-\s*(\d*))?", part)
        if not m:
            raise ConversionError(f"can't read the page range {part!r} (try 1-3, 5, 8-)")
        first = int(m.group(1))
        last = first if m.group(2) is None else int(m.group(2) or pages)
        if not 1 <= first <= last <= pages:
            raise ConversionError(f"pages {part} aren't in this {pages}-page PDF")
        ranges.append((first, last))
    return ranges


def split(ctx: Context, inputs: list[Path], params: Params) -> Path:
    src = inputs[0]
    pdf.require_unlocked(src)
    pages = page_count(ctx, src)
    ranges = (parse_ranges(params["pages"], pages) if params["pages"]
              else [(n, n) for n in range(1, pages + 1)])
    if len(ranges) == 1 and ranges[0] == (1, pages):
        raise ConversionError("that's the whole PDF: nothing to split off")
    folder = ctx.workdir / "split"
    folder.mkdir()
    stem = f.stem_of(src)
    width = len(str(pages))
    for i, (first, last) in enumerate(ranges):
        label = f"{first:0{width}d}" if first == last else f"{first:0{width}d}-{last:0{width}d}"
        part = folder / f"{stem} p{label}.pdf"
        qpdf(ctx, ["--empty", "--pages", src, f"{first}-{last}", "--", part], part)
        ctx.progress((i + 1) / len(ranges))
    return folder


def unlock(ctx: Context, inputs: list[Path], params: Params) -> Path:
    """A copy without the password (or, for a PDF that opens without one, without its limits
    on printing, copying and editing)."""
    src = inputs[0]
    state = pdf.known_encryption(src)
    if state == pdf.NOT_ENCRYPTED:
        raise ConversionError(f"{src.name} isn't password-protected")
    password = str(params.get("password") or "")
    if state == pdf.NEEDS_PASSWORD and not password:
        raise pdf.PasswordNeeded(f"{src.name} needs its password to be unlocked")
    out = ctx.workdir / "unlocked.pdf"
    _stdout, stderr, code = ctx.run_full(pdf.unlock_command(src, out), ok_codes=(0, 2, 3),
                                         stdin_data=password.encode())
    if code == 2 and pdf.wrong_password(stderr) and state == pdf.RESTRICTED_ONLY:
        # It opens without a password, and qpdf refuses one that isn't its owner password
        # (e.g. the password of another PDF unlocked in the same batch): go without.
        _stdout, stderr, code = ctx.run_full(pdf.unlock_command(src, out), ok_codes=(0, 2, 3),
                                             stdin_data=b"")
    if code == 2 and pdf.wrong_password(stderr):
        raise pdf.WrongPassword(f"wrong password for {src.name}")
    if code == 2 or not out.exists():
        raise ConversionError(f"couldn't unlock {src.name}: "
                              f"{stderr.strip().splitlines()[-1] if stderr.strip() else code}")
    return out


MERGE = Tool(
    "merge-pdf", "Merge PDF", short="Merge", done="Merged into one PDF",
    sources=MERGE_SOURCES, engine=merge, combine=True, min_files=2,
    suffix=lambda p: "merged", icon="merge",
    help="one PDF from several PDFs and/or images, in the order given",
)
SPLIT = Tool(
    "split-pdf", "Split PDF", short="Split", done="PDF split",
    sources=frozenset({"pdf"}), engine=split,
    suffix=lambda p: "split", icon="split",
    params=(Param("pages", "Pages", "str", "", "ranges like 1-3, 5, 8- (blank: every page)"),),
    help="separate PDFs for every page or for page ranges",
)
UNLOCK = Tool(
    "unlock", "Unlock PDF", short="Unlock", done="PDF unlocked",
    sources=frozenset({"pdf"}), engine=unlock, suffix=lambda p: "unlocked",
    icon="object-unlocked",
    params=(Param("password", "Password", "password", "",
                  "the PDF's password (blank if it opens without one)"),),
    help="a copy without the password, or without limits on printing and copying",
)

"""PDF merge (PDFs and images, in the order given), split (every page, or ranges) and unlock
(remove a password or restrictions)."""

from __future__ import annotations

import re
from pathlib import Path

from .. import formats as f
from ..engines import images, pdf
from ..i18n import N_, _, ngettext
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
        raise ConversionError(_("qpdf didn't write a PDF"))
    if code == 3:
        repaired_note(ctx)


def repaired_note(ctx: Context) -> None:
    text = _("a PDF was damaged; qpdf repaired it, so check the result")
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
            raise ConversionError(_("can't read the page range {range!r} (try 1-3, 5, 8-)")
                                  .format(range=part))
        first = int(m.group(1))
        last = first if m.group(2) is None else int(m.group(2) or pages)
        if not 1 <= first <= last <= pages:
            raise ConversionError(ngettext("pages {range} aren't in this {n}-page PDF",
                                           "pages {range} aren't in this {n}-page PDF", pages)
                                  .format(range=part, n=pages))
        ranges.append((first, last))
    return ranges


def split(ctx: Context, inputs: list[Path], params: Params) -> Path:
    src = inputs[0]
    pdf.require_unlocked(src)
    pages = page_count(ctx, src)
    ranges = (parse_ranges(params["pages"], pages) if params["pages"]
              else [(n, n) for n in range(1, pages + 1)])
    if len(ranges) == 1 and ranges[0] == (1, pages):
        raise ConversionError(_("that's the whole PDF: nothing to split off"))
    folder = ctx.workdir / "split"
    folder.mkdir()
    stem = f.stem_of(src)
    width = len(str(pages))
    for i, (first, last) in enumerate(ranges):
        label = f"{first:0{width}d}" if first == last else f"{first:0{width}d}-{last:0{width}d}"
        # Translators: "p" for page(s): "report p03.pdf", "report p05-08.pdf".
        part = folder / (_("{name} p{pages}").format(name=stem, pages=label) + ".pdf")
        qpdf(ctx, ["--empty", "--pages", src, f"{first}-{last}", "--", part], part)
        ctx.progress((i + 1) / len(ranges))
    return folder


def unlock(ctx: Context, inputs: list[Path], params: Params) -> Path:
    """A copy without the password (or, for a PDF that opens without one, without its limits
    on printing, copying and editing)."""
    src = inputs[0]
    state = pdf.known_encryption(src)
    if state == pdf.NOT_ENCRYPTED:
        raise ConversionError(_("{name} isn't password-protected").format(name=src.name))
    password = str(params.get("password") or "")
    if state == pdf.NEEDS_PASSWORD and not password:
        raise pdf.PasswordNeeded(_("{name} needs its password to be unlocked")
                                 .format(name=src.name))
    out = ctx.workdir / "unlocked.pdf"
    _stdout, stderr, code = ctx.run_full(pdf.unlock_command(src, out), ok_codes=(0, 2, 3),
                                         stdin_data=password.encode())
    if code == 2 and pdf.wrong_password(stderr) and state == pdf.RESTRICTED_ONLY:
        # It opens without a password, and qpdf refuses one that isn't its owner password
        # (e.g. the password of another PDF unlocked in the same batch): go without.
        _stdout, stderr, code = ctx.run_full(pdf.unlock_command(src, out), ok_codes=(0, 2, 3),
                                             stdin_data=b"")
    if code == 2 and pdf.wrong_password(stderr):
        raise pdf.WrongPassword(_("wrong password for {name}").format(name=src.name))
    if code == 2 or not out.exists():
        raise ConversionError(_("couldn't unlock {name}: {reason}").format(
            name=src.name, reason=stderr.strip().splitlines()[-1] if stderr.strip() else code))
    return out


MERGE = Tool(
    "merge-pdf", N_("Merge PDF"), short=N_("Merge"), done=N_("Merged into one PDF"),
    sources=MERGE_SOURCES, engine=merge, combine=True, min_files=2,
    suffix=lambda p: _("merged"), icon="merge",
    help=N_("one PDF from several PDFs and/or images, in the order given"),
)
SPLIT = Tool(
    "split-pdf", N_("Split PDF"), short=N_("Split"), done=N_("PDF split"),
    sources=frozenset({"pdf"}), engine=split,
    suffix=lambda p: _("split"), icon="split",
    params=(Param("pages", N_("Pages"), "str", "",
                  N_("ranges like 1-3, 5, 8- (blank: every page)")),),
    help=N_("separate PDFs for every page or for page ranges"),
)
UNLOCK = Tool(
    "unlock", N_("Unlock PDF"), short=N_("Unlock"), done=N_("PDF unlocked"),
    sources=frozenset({"pdf"}), engine=unlock, suffix=lambda p: _("unlocked"),
    icon="object-unlocked",
    params=(Param("password", N_("Password"), "password", "",
                  N_("the PDF's password (blank if it opens without one)")),),
    help=N_("a copy without the password, or without limits on printing and copying"),
)

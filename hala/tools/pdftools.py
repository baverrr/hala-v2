"""PDF merge (PDFs and images, in the order given) and split (every page, or ranges)."""

from __future__ import annotations

import re
from pathlib import Path

from .. import formats as f
from ..engines import images
from ..runner import Context, ConversionError
from . import Param, Params, Tool

MERGE_SOURCES = frozenset({"pdf", *f.IMAGE_IN})


def merge(ctx: Context, inputs: list[Path], params: Params) -> Path:
    parts: list[Path] = []
    for i, src in enumerate(inputs):
        ctx.check_cancelled()
        if f.ext_of(src) == "pdf":
            parts.append(src)
        else:
            page = images.images_to_pdf(ctx, [src])  # always workdir/out.pdf: move it aside
            parts.append(page.rename(ctx.workdir / f"image{i}.pdf"))
        ctx.progress(0.5 * (i + 1) / len(inputs))
    out = ctx.workdir / "merged.pdf"
    ctx.run(["qpdf", "--empty", "--pages", *parts, "--", out])
    return out


def page_count(ctx: Context, src: Path) -> int:
    return int(ctx.run(["qpdf", "--show-npages", src]).strip())


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
        ctx.run(["qpdf", "--empty", "--pages", src, f"{first}-{last}", "--",
                 folder / f"{stem} p{label}.pdf"])
        ctx.progress((i + 1) / len(ranges))
    return folder


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

"""PDF to page images or text via poppler."""

from __future__ import annotations

from pathlib import Path

from ..runner import Context, ConversionError

RENDER_DPI = 150


def to_images(ctx: Context, src: Path, dst_ext: str) -> Path:
    """One image per page. A single-page PDF gives a file; longer ones give a folder."""
    pages = ctx.workdir / "pages"
    pages.mkdir()
    fmt = ["-jpeg", "-jpegopt", "quality=90"] if dst_ext == "jpg" else ["-png"]
    ctx.run(["pdftoppm", *fmt, "-r", str(RENDER_DPI), src, pages / "page"])
    produced = sorted(pages.iterdir())
    if not produced:
        raise ConversionError("this PDF has no pages to render")
    return produced[0] if len(produced) == 1 else pages


def to_text(ctx: Context, src: Path, dst_ext: str) -> Path:
    out = ctx.workdir / "out.txt"
    ctx.run(["pdftotext", "-layout", "-enc", "UTF-8", src, out])
    return out

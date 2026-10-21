"""Read QR codes and barcodes from images (and PDF pages) with zbar."""

from __future__ import annotations

from pathlib import Path

from .. import formats as f
from ..engines import images
from ..runner import Context, ConversionError
from . import Params, Tool

NOTHING_FOUND = 4  # zbarimg's exit code when no symbol was found
QR_RENDER_DPI = 200


def read(ctx: Context, inputs: list[Path], params: Params) -> Path:
    src = inputs[0]
    pictures: list[Path] = [src]
    if f.ext_of(src) == "pdf":
        pages = ctx.workdir / "pages"
        pages.mkdir()
        ctx.run(["pdftoppm", "-png", "-r", str(QR_RENDER_DPI), src, pages / "p"])
        pictures = sorted(pages.iterdir())
    else:
        images.check_picture(src)  # zbarimg reads through ImageMagick, which sniffs content
        if f.ext_of(src) not in ("jpg", "png", "gif", "bmp", "tiff"):
            # ImageMagick may lack HEIC/AVIF/WebP; give zbarimg a PNG.
            png = ctx.workdir / "in.png"
            ctx.run(["vips", "copy", src, png])
            pictures = [png]
    out, _err, code = ctx.run_full(["zbarimg", "-q", "--raw", *pictures],
                                   ok_codes=(0, NOTHING_FOUND))
    codes = [line for line in out.splitlines() if line.strip()]
    if code == NOTHING_FOUND or not codes:
        raise ConversionError("no QR code or barcode found")
    result = ctx.workdir / "codes.txt"
    result.write_text("\n".join(codes) + "\n")
    return result


TOOL = Tool(
    "read-qr", "Read QR", done="QR code read",
    sources=frozenset({*f.IMAGE_IN - {"svg"}, "pdf"}), engine=read,
    suffix=lambda p: "codes", icon="view-barcode-qr", shows_text=True,
    help="decode QR codes and barcodes into a text file (also shown in the notification)",
)

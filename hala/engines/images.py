"""Still and animated images via libvips; images to PDF via a small built-in PDF writer."""

from __future__ import annotations

import zlib
from pathlib import Path

from .. import limits
from ..formats import ANIMATED, ext_of
from ..runner import Context, ConversionError

SAVE_OPTIONS = {
    "jpg": "[Q=90,optimize-coding,strip=false]",
    "webp": "[Q=85]",
    "heic": "[Q=60]",   # libheif's quality scale runs lower than JPEG's
    "avif": "[Q=55]",
    "png": "[compression=6]",
    "tiff": "[compression=deflate]",
    "gif": "",
}
NO_ALPHA = frozenset({"jpg"})
# How far into an SVG to look for its <svg> tag (after the XML prolog, comments, a doctype).
SVG_SNIFF = 4096


def check_picture(path: Path) -> None:
    """Refuse a "picture" whose bytes aren't one of the formats we read.

    vips and Qt pick a decoder by content, not by name, so a PostScript or ImageMagick
    script file named photo.jpg would otherwise reach ImageMagick (and through it
    Ghostscript). A mislabelled picture (a PNG called .jpg) is still fine.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(SVG_SNIFF)
    except OSError as exc:
        raise ConversionError(f"can't read {path.name}: {exc.strerror}") from None
    if _raster_signature(head) or _looks_like_svg(head):
        return
    raise ConversionError(f"{path.name} isn't a picture Hala can read (its contents don't "
                          "match any image format)")


def _raster_signature(head: bytes) -> bool:
    return (head.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a",
                             b"BM", b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+",
                             b"\xff\x0a", b"\x00\x00\x00\x0cJXL \r\n\x87\n"))
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
            or head[4:8] == b"ftyp")  # HEIC and AVIF (ISO base media)


def _looks_like_svg(head: bytes) -> bool:
    text = head.removeprefix(b"\xef\xbb\xbf").lstrip()
    return text.startswith(b"<") and b"<svg" in text.lower()


def _header(ctx: Context, path: Path, field: str) -> str:
    return ctx.run(["vipsheader", "-f", field, path]).strip()


def check_input(ctx: Context, path: Path) -> None:
    """A real picture (check_picture), and not one so large that it would fill the disk."""
    check_picture(path)
    try:
        width, height = int(_header(ctx, path, "width")), int(_header(ctx, path, "height"))
    except ValueError:
        return  # vips will report what's wrong with it
    limits.check_pixels(width, height, path.name)


def _normalize(ctx: Context, src: Path, flatten: bool) -> Path:
    """Apply EXIF rotation, turn CMYK into sRGB, and white-flatten alpha if asked."""
    staged = ctx.workdir / "rotated.v"
    ctx.run(["vips", "autorot", src, staged])
    if "CMYK" in _header(ctx, staged, "interpretation"):
        # Otherwise C, M, Y, K would be read as R, G, B, alpha.
        srgb = ctx.workdir / "from-cmyk.v"
        ctx.run(["vips", "colourspace", staged, srgb, "srgb"])
        staged = srgb
    if flatten and int(_header(ctx, staged, "bands")) in (2, 4):
        # White is 65535 in a 16-bit image; 255 there would be nearly black.
        white = "65535" if "USHORT" in _header(ctx, staged, "format") else "255"
        flat = ctx.workdir / "flat.v"
        ctx.run(["vips", "flatten", staged, flat, "--background", white])
        return flat
    return staged


def convert(ctx: Context, src: Path, dst_ext: str) -> Path:
    check_input(ctx, src)
    if dst_ext == "mp4":
        return to_video(ctx, src, dst_ext)
    src_ext = ext_of(src)
    out = ctx.workdir / f"out.{dst_ext}"
    if src_ext in ANIMATED and dst_ext in ANIMATED:
        # Keep every frame of an animation.
        ctx.run(["vips", "copy", f"{src}[n=-1]", f"{out}{SAVE_OPTIONS[dst_ext]}"])
        return out
    if src_ext == "svg":
        # Rasterize vectors at 2x so the result isn't tiny.
        loaded = ctx.workdir / "svg.v"
        ctx.run(["vips", "copy", f"{src}[scale=2]", loaded])
        src = loaded
    staged = _normalize(ctx, src, flatten=dst_ext in NO_ALPHA)
    ctx.progress(0.5)
    ctx.run(["vips", "copy", staged, f"{out}{SAVE_OPTIONS[dst_ext]}"])
    return out


# A still image becomes a video this long.
STILL_SECONDS = 5
EVEN = "scale=trunc(iw/2)*2:trunc(ih/2)*2"  # x264's 4:2:0 needs even dimensions


def _frames(ctx: Context, path: Path) -> int:
    out, _err, code = ctx.run_full(["vipsheader", "-f", "n-pages", path], ok_codes=(0, 1))
    return int(out.strip()) if code == 0 and out.strip().isdigit() else 1


def to_video(ctx: Context, src: Path, dst_ext: str) -> Path:
    """MP4 from an image: an animation plays as itself; a still is held for STILL_SECONDS."""
    check_input(ctx, src)
    out = ctx.workdir / "out.mp4"
    x264 = ["-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart"]
    if ext_of(src) in ANIMATED and _frames(ctx, src) > 1:
        gif = src
        if ext_of(src) != "gif":  # ffmpeg can't decode animated WebP; vips keeps the timing
            gif = ctx.workdir / "anim.gif"
            ctx.run(["vips", "copy", f"{src}[n=-1]", gif])
        ctx.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", gif, "-vf", EVEN, *x264, out])
        return out
    staged = _normalize(ctx, src, flatten=True)  # rotation applied, alpha on white
    frame = ctx.workdir / "frame.png"
    ctx.run(["vips", "copy", staged, frame])
    ctx.progress(0.3)
    ctx.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loop", "1", "-framerate", "30",
             "-i", frame, "-t", str(STILL_SECONDS), "-vf", EVEN, "-tune", "stillimage", *x264,
             out])
    return out


def to_pdf(ctx: Context, src: Path, dst_ext: str) -> Path:
    return images_to_pdf(ctx, [src])


# Page size: each image at 150 DPI.
PDF_DPI = 150


def images_to_pdf(ctx: Context, sources: list[Path]) -> Path:
    """One page per image: JPEG sources re-encoded as JPEG (Q95), the rest stored lossless."""
    pages: list[tuple[bytes, str, int, int]] = []
    for i, src in enumerate(sources):
        ctx.check_cancelled()
        check_input(ctx, src)
        src_ext = ext_of(src)
        if src_ext == "svg":
            loaded = ctx.workdir / f"svg{i}.v"
            ctx.run(["vips", "copy", f"{src}[scale=2]", loaded])
            src = loaded
        staged = _normalize(ctx, src, flatten=True)
        srgb = ctx.workdir / f"srgb{i}.v"
        ctx.run(["vips", "colourspace", staged, srgb, "srgb"])
        u8 = ctx.workdir / f"u8_{i}.v"
        ctx.run(["vips", "cast", srgb, u8, "uchar"])
        width = int(ctx.run(["vipsheader", "-f", "width", u8]))
        height = int(ctx.run(["vipsheader", "-f", "height", u8]))
        if src_ext == "jpg":
            jpg = ctx.workdir / f"page{i}.jpg"
            ctx.run(["vips", "jpegsave", u8, jpg, "--Q", "95"])
            pages.append((jpg.read_bytes(), "/DCTDecode", width, height))
        else:
            raw = ctx.workdir / f"page{i}.raw"
            ctx.run(["vips", "rawsave", u8, raw])
            pages.append((zlib.compress(raw.read_bytes(), 6), "/FlateDecode", width, height))
        for scratch in ctx.workdir.glob("*.v"):
            scratch.unlink()
        ctx.progress((i + 1) / len(sources))
    out = ctx.workdir / "out.pdf"
    out.write_bytes(_build_pdf(pages))
    return out


def _build_pdf(pages: list[tuple[bytes, str, int, int]]) -> bytes:
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled in once the page tree exists
    tree = add(b"")
    page_ids = []
    for data, filt, width, height in pages:
        image = add(b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB"
                    b" /BitsPerComponent 8 /Filter %s /Length %d >>\nstream\n%s\nendstream"
                    % (width, height, filt.encode(), len(data), data))
        w_pt, h_pt = width * 72 / PDF_DPI, height * 72 / PDF_DPI
        draw = b"q %.2f 0 0 %.2f 0 0 cm /Im0 Do Q" % (w_pt, h_pt)
        content = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(draw), draw))
        page_ids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f]"
                            b" /Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
                            % (tree, w_pt, h_pt, image, content)))
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % tree
    kids = b" ".join(b"%d 0 R" % p for p in page_ids)
    objects[tree - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids))

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for num, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (num, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, catalog, xref)
    return bytes(out)

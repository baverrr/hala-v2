"""PDF to page images or text via poppler; password-protected PDFs via qpdf."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .. import limits
from ..i18n import _
from ..runner import Context, ConversionError

# qpdf --requires-password exit codes, and UNKNOWN when qpdf couldn't be asked
NEEDS_PASSWORD, NOT_ENCRYPTED, RESTRICTED_ONLY, UNKNOWN = 0, 2, 3, -1
CHECK_TIMEOUT_S = 30
POPPLER_TIMEOUT_S = 300
# Rendering every page: more pages than this, or more pixels in all, is refused up front
# (a few KB of PDF can describe thousands of huge blank pages: hours of work, gigabytes out).
MAX_RENDER_PAGES = 5000
MAX_RENDER_PIXELS = 20_000_000_000  # ~9000 A4 pages at 150 dpi
# Bytes per pixel to expect when checking free space: a guess between text (much less) and
# photos (a little more).
RENDER_BYTES_PER_PIXEL = {"png": 0.5, "jpg": 0.2}
# pdfinfo -f/-l lines: "Page    1 size: 595 x 842 pts (A4)" and "Page    1 rot:  90"
PAGE_LINE = re.compile(r"^Page\s+(?P<page>\d+)\s+(?P<what>size|rot):\s+(?P<w>[\d.]+)"
                       r"(?:\s+x\s+(?P<h>[\d.]+))?")


class PasswordNeeded(ConversionError):
    pass


class WrongPassword(ConversionError):
    pass


def _check(path: Path) -> tuple[int, str]:
    """(state, why it's UNKNOWN)."""
    try:
        code = subprocess.run(["qpdf", "--requires-password", str(path)], capture_output=True,
                              timeout=CHECK_TIMEOUT_S).returncode
    except FileNotFoundError:
        return UNKNOWN, "qpdf isn't installed (apt install qpdf)"
    except subprocess.TimeoutExpired:
        return UNKNOWN, f"qpdf gave no answer within {CHECK_TIMEOUT_S} s"
    except OSError as exc:
        return UNKNOWN, f"qpdf couldn't run ({exc.strerror or exc})"
    return (code if code in (NEEDS_PASSWORD, RESTRICTED_ONLY) else NOT_ENCRYPTED), ""


def encryption(path: Path) -> int:
    """NEEDS_PASSWORD (it won't open without one), RESTRICTED_ONLY (opens, but with limits
    such as no printing), NOT_ENCRYPTED, or UNKNOWN when qpdf is missing or timed out. A file
    qpdf can't read counts as not encrypted, so the tool that reads it reports the real
    problem."""
    return _check(path)[0]


def known_encryption(path: Path) -> int:
    """encryption(), but a clear error instead of UNKNOWN, for the places that act on the
    answer (unlocking, opening for editing)."""
    state, why = _check(path)
    if state == UNKNOWN:
        raise ConversionError(_("couldn't check whether {name} is password-protected: {reason}")
                              .format(name=path.name, reason=why))
    return state


def require_unlocked(path: Path) -> None:
    """A clear error for a PDF that needs a password, before poppler or Ghostscript give a
    cryptic one. When qpdf can't tell, the reading tool's own error has to do."""
    if encryption(path) == NEEDS_PASSWORD:
        raise PasswordNeeded(_("{name} is password-protected: unlock it first (Hala tools → "
                               "Unlock PDF, or hala tool unlock)").format(name=path.name))


def unlock_command(src: Path, out: Path) -> list[str]:
    """qpdf reading the password from its input: never on the command line, where other users
    could see it with ps (and qpdf's AppArmor profile only lets it open PDF-like names)."""
    return ["qpdf", "--password-file=-", "--decrypt", str(src), str(out)]


def wrong_password(stderr: str) -> bool:
    return "invalid password" in stderr.lower()


def poppler(cmd: list[str], failure: str) -> str:
    """Run a poppler tool and return its output; failure starts the error message."""
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True,
                              timeout=POPPLER_TIMEOUT_S).stdout
    except FileNotFoundError:
        raise ConversionError(failure + ": " + _("poppler isn't installed (apt install "
                                                 "poppler-utils)")) from None
    except subprocess.TimeoutExpired:
        raise ConversionError(failure + ": " + _("it took too long")) from None
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()
        raise ConversionError(failure + (f" ({detail[-1]})" if detail else "")) from None


def page_sizes(path: Path, name: str | None = None) -> list[tuple[float, float]]:
    """Each page's size in points as it's displayed: a page rotated by 90 or 270 degrees has
    its width and height swapped, like the picture pdftoppm renders of it."""
    name = name or path.name
    failure = _("can't open {name} as a PDF").format(name=name)
    info = poppler(["pdfinfo", str(path)], failure)
    count = next((int(fs[1]) for line in info.splitlines() if (fs := line.split())[:1]
                  == ["Pages:"] and len(fs) > 1 and fs[1].isdigit()), 0)
    if count < 1:
        raise ConversionError(_("{name} has no pages").format(name=name))
    if count > MAX_RENDER_PAGES * 20:
        raise ConversionError(_("{name} has {n:,} pages; that's more than Hala handles").format(
            name=name, n=count))
    info = poppler(["pdfinfo", "-f", "1", "-l", str(count), str(path)], failure)
    sizes: dict[int, tuple[float, float]] = {}
    turned: set[int] = set()
    for line in info.splitlines():
        m = PAGE_LINE.match(line)
        if m and m["what"] == "size":
            sizes[int(m["page"])] = (float(m["w"]), float(m["h"]))
        elif m and m["what"] == "rot" and int(float(m["w"])) % 180 == 90:
            turned.add(int(m["page"]))
    if len(sizes) != count:
        raise ConversionError(failure + " " + _("(couldn't read its page sizes)"))
    return [(h, w) if n in turned else (w, h) for n, (w, h) in sorted(sizes.items())]


def page_pixels(size: tuple[float, float], dpi: int) -> tuple[int, int]:
    """The picture pdftoppm makes of a page this size (points) at dpi."""
    return round(size[0] * dpi / 72), round(size[1] * dpi / 72)


def check_render(src: Path, dpi: int, ext: str, outdir: Path) -> None:
    """Refuse, before rendering anything, a PDF whose pages would be too big, too many, or
    wouldn't fit on the disk as pictures."""
    sizes = page_sizes(src)
    if len(sizes) > MAX_RENDER_PAGES:
        raise ConversionError(_("{name} has {n:,} pages; making a picture of each is limited "
                                "to {limit:,}").format(name=src.name, n=len(sizes),
                                                       limit=MAX_RENDER_PAGES))
    total = 0
    for i, size in enumerate(sizes):
        width, height = page_pixels(size, dpi)
        limits.check_pixels(width, height, f"page {i + 1} of {src.name}")
        total += width * height
    if total > MAX_RENDER_PIXELS:
        raise ConversionError(_("{name}'s pages would make {total:,.0f} gigapixels of "
                                "pictures; the limit is {limit:,.0f}").format(
            name=src.name, total=total / 1e9, limit=MAX_RENDER_PIXELS / 1e9))
    need = int(total * RENDER_BYTES_PER_PIXEL.get(ext, 0.5)) + limits.MIN_FREE
    try:
        free = limits.free_space(outdir)
    except OSError:
        return
    if need > free:
        raise ConversionError(_("the pictures of {name}'s {n:,} pages would need about "
                                "{need}, but only {free} is free").format(
            name=src.name, n=len(sizes), need=limits.human(need), free=limits.human(free)))


RENDER_DPI = 150


def to_images(ctx: Context, src: Path, dst_ext: str) -> Path:
    """One image per page. A single-page PDF gives a file; longer ones give a folder."""
    require_unlocked(src)
    check_render(src, RENDER_DPI, dst_ext, ctx.workdir)
    pages = ctx.workdir / "pages"
    pages.mkdir()
    fmt = ["-jpeg", "-jpegopt", "quality=90"] if dst_ext == "jpg" else ["-png"]
    ctx.run(["pdftoppm", *fmt, "-r", str(RENDER_DPI), src, pages / "page"])
    produced = sorted(pages.iterdir())
    if not produced:
        raise ConversionError(_("this PDF has no pages to render"))
    return produced[0] if len(produced) == 1 else pages


def to_text(ctx: Context, src: Path, dst_ext: str) -> Path:
    require_unlocked(src)
    out = ctx.workdir / "out.txt"
    ctx.run(["pdftotext", "-layout", "-enc", "UTF-8", src, out])
    return out

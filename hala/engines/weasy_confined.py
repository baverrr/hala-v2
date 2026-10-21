"""Render an HTML file to PDF with WeasyPrint, reading only pictures inside the given folders.

    python -I -m hala.engines.weasy_confined IN.html OUT.pdf BASE ROOT...

WeasyPrint fetches whatever a page or an SVG picture inside it refers to. Pictures here come
from a downloaded document's folder, and a crafted SVG could point at ~/Pictures/... (which
would end up in the PDF) or at a web server. So the only things it may open are data: URLs
and pictures or fonts under one of the ROOT folders, outside hidden folders (a document saved
in the home folder mustn't reach ~/.ssh); anything else is refused and left out. BASE is the
folder relative links are resolved against (the document's own). Hala's own print stylesheet
(hala/data/print.css: page numbers, readable type, tables, code) is added as the page's last
style, so it wins over the little CSS pandoc still writes.

Each picture WeasyPrint couldn't load is printed as a "left-out<TAB>URL" line.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

PRINT_CSS = Path(__file__).resolve().parent.parent / "data" / "print.css"
LEFT_OUT = "left-out\t"
# Fonts a stylesheet may load: TrueType, OpenType, WOFF, WOFF2, TrueType collections.
FONT_SIGNATURES = (b"\x00\x01\x00\x00", b"OTTO", b"true", b"wOFF", b"wOF2", b"ttcf")


def inside(path: Path, roots: list[Path]) -> bool:
    """Whether path is under one of roots, with no hidden folder or file on the way."""
    return any(path.is_relative_to(root)
               and not any(part.startswith(".") for part in path.relative_to(root).parts)
               for root in roots)


def servable(path: Path) -> bool:
    """Pictures and fonts only: a link can't make WeasyPrint attach any other file."""
    from .images import SVG_SNIFF, _looks_like_svg, _raster_signature

    try:
        with open(path, "rb") as fh:
            head = fh.read(SVG_SNIFF)
    except OSError:
        return False
    return _raster_signature(head) or _looks_like_svg(head) or head.startswith(FONT_SIGNATURES)


def confined(roots: list[Path], fetch: Callable[..., Any]) -> Callable[..., Any]:
    """fetch, but only for data: URLs and pictures or fonts under roots (see inside)."""

    def fetcher(url: str, *args: Any, **kwargs: Any) -> Any:
        parsed = urlparse(url)
        if parsed.scheme == "data":
            return fetch(url, *args, **kwargs)
        if parsed.scheme == "file" and parsed.netloc in ("", "localhost"):
            path = Path(unquote(parsed.path)).resolve()
            if inside(path, roots) and servable(path):
                return fetch(url, *args, **kwargs)
        raise ValueError(f"not allowed to read {url}")

    return fetcher


class _LeftOut(logging.Handler):
    """Prints the pictures WeasyPrint gave up on (it only logs them)."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.ERROR:
            return
        msg = record.msg if isinstance(record.msg, str) else ""
        if msg.startswith("Failed to load image") and record.args:
            what = str(record.args[0] if isinstance(record.args, tuple) else record.args)
        elif msg.startswith("Failed to load inline SVG"):
            what = "an SVG picture"
        else:
            return
        print(LEFT_OUT + what.replace("\n", " "), flush=True)


def with_print_style(html: str) -> str:
    """The page with Hala's print stylesheet as its last style (an author style, so it wins
    over pandoc's at the same specificity; WeasyPrint's own stylesheets= are user styles,
    which always lose)."""
    style = f"<style>\n{PRINT_CSS.read_text(encoding='utf-8')}</style>\n"
    at = html.find("</head>")
    return html[:at] + style + html[at:] if at >= 0 else style + html


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        sys.exit(__doc__)
    html, pdf, base, *roots = argv
    from weasyprint import HTML, default_url_fetcher

    logging.getLogger("weasyprint").addHandler(_LeftOut())
    allowed = [Path(r).resolve() for r in roots]
    fetcher = confined(allowed, default_url_fetcher)
    page = with_print_style(Path(html).read_text(encoding="utf-8"))
    HTML(string=page, base_url=Path(base).resolve().as_uri() + "/",
         url_fetcher=fetcher).write_pdf(pdf)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

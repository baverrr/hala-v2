"""Render an HTML file to PDF with WeasyPrint, reading only files inside the given folders.

    python -I -m hala.engines.weasy_confined IN.html OUT.pdf BASE ROOT...

WeasyPrint fetches whatever a page or an SVG picture inside it refers to. Pictures here come
from a downloaded document's folder, and a crafted SVG could point at ~/Pictures/... (which
would end up in the PDF) or at a web server. So the only things it may open are data: URLs
and files under one of the ROOT folders; anything else is refused and left out. BASE is the
folder relative links are resolved against (the document's own).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


def confined(roots: list[Path], fetch: Callable[..., Any]) -> Callable[..., Any]:
    """fetch, but only for data: URLs and local files under roots."""

    def fetcher(url: str, *args: Any, **kwargs: Any) -> Any:
        parsed = urlparse(url)
        if parsed.scheme == "data":
            return fetch(url, *args, **kwargs)
        if parsed.scheme == "file" and parsed.netloc in ("", "localhost"):
            path = Path(unquote(parsed.path)).resolve()
            if any(path.is_relative_to(root) for root in roots):
                return fetch(url, *args, **kwargs)
        raise ValueError(f"not allowed to read {url}")

    return fetcher


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        sys.exit(__doc__)
    html, pdf, base, *roots = argv
    from weasyprint import HTML, default_url_fetcher

    allowed = [Path(r).resolve() for r in roots]
    HTML(filename=html, base_url=Path(base).resolve().as_uri() + "/",
         url_fetcher=confined(allowed, default_url_fetcher)).write_pdf(pdf)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

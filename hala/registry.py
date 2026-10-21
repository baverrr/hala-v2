"""The conversion table: which outputs each input type offers, and which engine does the work.

The CLI and the Dolphin service menu read from this one table (the phase-3 drop wheel will too).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from . import formats as f
from .engines import archives, documents, images, media, pdf
from .runner import Context

Engine = Callable[[Context, Path, str], Path]
ANY = "*"


@dataclass(frozen=True)
class Route:
    sources: frozenset[str]  # input extensions, or {ANY}
    targets: tuple[str, ...]
    engine: Engine
    label: str

    def __post_init__(self) -> None:
        if not self.targets:
            raise ValueError(f"{self.label} route has no targets")


def _route(sources: Iterable[str], targets: Iterable[str], engine: Engine, label: str) -> Route:
    return Route(frozenset(sources), tuple(targets), engine, label)


# Earlier routes win when two offer the same source -> target pair.
ROUTES: tuple[Route, ...] = (
    _route(f.IMAGE_IN, f.IMAGE_OUT, images.convert, "libvips"),
    _route(f.IMAGE_IN, ("pdf",), images.to_pdf, "built-in PDF writer"),
    _route({"pdf"}, ("png", "jpg"), pdf.to_images, "poppler"),
    _route({"pdf"}, ("txt",), pdf.to_text, "poppler"),
    _route(f.VIDEO_IN, (*f.VIDEO_OUT, *f.AUDIO_OUT), media.convert_video, "ffmpeg"),
    _route(f.AUDIO_IN, f.AUDIO_OUT, media.convert_audio, "ffmpeg"),
    _route(f.SUBTITLE_IN, f.SUBTITLE_OUT, media.convert_subtitle, "ffmpeg"),
    _route(f.WORD_IN, f.WORD_OUT, documents.office, "LibreOffice"),
    _route(f.SHEET_IN, f.SHEET_OUT, documents.office, "LibreOffice"),
    _route(f.SLIDES_IN, f.SLIDES_OUT, documents.office, "LibreOffice"),
    _route(f.MARKUP_IN, f.MARKUP_OUT, documents.pandoc, "pandoc"),
    _route({"docx", "odt"}, ("md", "html", "epub"), documents.pandoc, "pandoc"),
    _route(f.ARCHIVE_IN, f.PACK_OUT, archives.pack, "7-Zip / tar"),
    _route({ANY}, f.PACK_OUT, archives.pack, "7-Zip / tar"),
)
# "folder" as a target means extract, which is a different job than re-packing.
_EXTRACT = _route(f.ARCHIVE_IN, (f.FOLDER,), archives.extract, "7-Zip / tar")


def _matches(route: Route, ext: str) -> bool:
    return ext in route.sources or ANY in route.sources


def route_for(src_ext: str, dst_ext: str) -> Route | None:
    if dst_ext == f.FOLDER:
        return _EXTRACT if src_ext in _EXTRACT.sources else None
    for route in ROUTES:
        if _matches(route, src_ext) and dst_ext in route.targets and dst_ext != src_ext:
            return route
    return None


def targets_for(src_ext: str) -> list[str]:
    """Every output this input can become, in table order, without duplicates or itself."""
    # Extracting comes first: it's what an archive is usually opened for.
    seen: dict[str, None] = {f.FOLDER: None} if src_ext in _EXTRACT.sources else {}
    for route in ROUTES:
        if _matches(route, src_ext):
            for target in route.targets:
                if target != src_ext and route_for(src_ext, target) is not None:
                    seen.setdefault(target, None)
    return list(seen)


def common_targets(src_exts: list[str]) -> list[str]:
    """Outputs every one of the inputs supports (for batch drops of mixed files)."""
    if not src_exts:
        return []
    first, *rest = (targets_for(e) for e in dict.fromkeys(src_exts))
    return [t for t in first if all(t in other for other in rest)]

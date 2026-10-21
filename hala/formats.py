"""File-type detection by extension, and the format groups the routes are built from."""

from __future__ import annotations

from pathlib import Path

# Multi-part suffixes that must win over their last component (".gz").
COMPOUND_EXTS = ("tar.gz", "tar.bz2", "tar.xz", "tar.zst")
ALIASES = {"jpeg": "jpg", "tif": "tiff", "heif": "heic", "aif": "aiff", "tgz": "tar.gz",
           "htm": "html", "markdown": "md", "mpeg": "mpg"}

# Pseudo-extension for "extract this archive into a folder".
FOLDER = "folder"

IMAGE_IN = frozenset({"jpg", "png", "heic", "webp", "tiff", "bmp", "gif", "avif", "svg", "jxl"})
# MP4 makes a short video of a still, or plays an animation (BMP is accepted, not offered).
IMAGE_OUT = ("jpg", "png", "heic", "webp", "tiff", "mp4", "gif", "avif")
ANIMATED = frozenset({"gif", "webp"})

VIDEO_IN = frozenset({"mp4", "mov", "mkv", "avi", "wmv", "webm", "m4v", "flv", "mpg", "3gp",
                      "ts", "mts"})
VIDEO_OUT = ("mp4", "mov", "mkv", "webm", "avi", "wmv", "gif")

AUDIO_IN = frozenset({"mp3", "m4a", "aac", "wav", "flac", "ogg", "opus", "aiff", "wma", "mka"})
AUDIO_OUT = ("mp3", "m4a", "wav", "flac", "ogg", "opus", "aiff")

SUBTITLE_IN = frozenset({"srt", "vtt", "ass", "ssa"})
SUBTITLE_OUT = ("srt", "vtt", "ass")

WORD_IN = frozenset({"docx", "doc", "odt", "rtf", "pages", "wpd", "txt"})
WORD_OUT = ("pdf", "docx", "odt", "rtf", "txt")
SHEET_IN = frozenset({"xlsx", "xls", "ods", "csv", "numbers"})
SHEET_OUT = ("pdf", "xlsx", "ods", "csv")
SLIDES_IN = frozenset({"pptx", "ppt", "odp", "key"})
SLIDES_OUT = ("pdf", "pptx", "odp")

# Markup-ish formats pandoc reads well; .docx/.odt are handled by LibreOffice first.
MARKUP_IN = frozenset({"md", "html", "epub", "rst", "org", "tex", "ipynb"})
MARKUP_OUT = ("md", "html", "epub", "docx", "odt", "txt", "pdf")

PDF_OUT = ("png", "jpg", "txt")

ARCHIVE_IN = frozenset({"zip", "7z", "rar", "tar", "gz", *COMPOUND_EXTS})
PACK_OUT = ("zip", "tar.gz", "7z")

# How targets are named in menus and on the wheel (default: the extension in capitals).
LABELS = {FOLDER: "Folder (extract)", "tar.gz": "TAR.GZ", "txt": "Text"}


def label(target: str) -> str:
    return LABELS.get(target, target.upper())


# Format families: the Dolphin multi-file menus, and the tie-break when an extension's glob
# names several MIME types (".ts" is both MPEG-TS video and a Qt Linguist file).
FAMILIES: dict[str, frozenset[str]] = {
    "image": IMAGE_IN, "video": VIDEO_IN, "audio": AUDIO_IN, "subtitle": SUBTITLE_IN,
    "document": WORD_IN, "spreadsheet": SHEET_IN, "presentation": SLIDES_IN,
    "markup": MARKUP_IN, "pdf": frozenset({"pdf"}), "archive": ARCHIVE_IN,
}


def normalize_ext(ext: str) -> str:
    """A user-typed extension (".JPEG", "tgz") in the form the routes use ("jpg", "tar.gz")."""
    ext = ext.lower().lstrip(".")
    return ALIASES.get(ext, ext)


def split_name(name: str) -> tuple[str, str]:
    """("backup", ".tar.gz") for "backup.tar.gz"; the extension keeps its original case."""
    for compound in COMPOUND_EXTS:
        if name.lower().endswith("." + compound):
            return name[: -len(compound) - 1], name[-len(compound) - 1:]
    stem, dot, ext = name.rpartition(".")
    return (stem, dot + ext) if stem else (name, "")


def ext_of(path: Path) -> str:
    """Normalized extension without the dot ("" if none); directories report FOLDER."""
    if path.is_dir():
        return FOLDER
    return normalize_ext(split_name(path.name)[1])


def stem_of(path: Path) -> str:
    """File name without its (possibly compound) extension; a folder's whole name."""
    return path.name if path.is_dir() else split_name(path.name)[0]

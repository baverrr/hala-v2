"""Metadata: remove all of it or just the location, and/or set title, author and comment.

Images and PDFs go through exiftool; audio and video through an ffmpeg stream copy (exiftool
can't write MKV, MP3 or FLAC). Blank fields keep their current value.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .. import formats as f
from ..runner import Context, ConversionError, local_inputs
from . import Param, Params, Tool

EXIFTOOL_TYPES = frozenset({"jpg", "png", "webp", "heic", "avif", "tiff", "gif", "pdf"})
MEDIA_TYPES = frozenset({*f.VIDEO_IN, *f.AUDIO_IN})
REMOVE = ("everything", "location", "nothing")
FIELDS = ("title", "author", "comment")
# Where each field lives: exiftool tag names (written wherever the format has them) and
# ffmpeg's container tag.
EXIF_TAGS = {"title": ("Title", "XPTitle"), "author": ("Artist", "Creator", "Author"),
             "comment": ("Comment", "UserComment", "Description")}
FFMPEG_TAGS = {"title": "title", "author": "artist", "comment": "comment"}
LOCATION_TAGS_FFMPEG = ("location", "location-eng", "com.apple.quicktime.location.ISO6709")


def current(path: Path) -> dict[str, str]:
    """What the file says now: title/author/comment, plus "location" if it has one."""
    ext = f.ext_of(path)
    found: dict[str, str] = {}
    try:
        if ext in EXIFTOOL_TYPES and shutil.which("exiftool"):
            out = subprocess.run(["exiftool", "-j", "-G0", "-n", str(path)], capture_output=True,
                                 text=True, timeout=20).stdout
            tags = json.loads(out)[0] if out.strip() else {}
            plain = {k.split(":", 1)[-1]: v for k, v in tags.items()}
            for field, names in EXIF_TAGS.items():
                value = next((plain[n] for n in names if plain.get(n)), "")
                if value:
                    found[field] = str(value)
            if "GPSLatitude" in plain or "GPSPosition" in plain:
                found["location"] = str(plain.get("GPSPosition")
                                        or f"{plain['GPSLatitude']}, {plain.get('GPSLongitude')}")
        elif ext in MEDIA_TYPES and shutil.which("ffprobe"):
            out = subprocess.run(local_inputs(["ffprobe", "-v", "error", "-print_format",
                                               "json", "-show_format", str(path)]),
                                 capture_output=True, text=True, timeout=20).stdout
            tags = {k.lower(): v for k, v in json.loads(out or "{}").get("format", {})
                    .get("tags", {}).items()}
            for field, tag in FFMPEG_TAGS.items():
                if tags.get(tag):
                    found[field] = tags[tag]
            loc = next((tags[t.lower()] for t in LOCATION_TAGS_FFMPEG if tags.get(t.lower())), "")
            if loc:
                found["location"] = loc
    except (OSError, ValueError, subprocess.TimeoutExpired, IndexError):
        pass
    return found


def edit(ctx: Context, inputs: list[Path], params: Params) -> Path:
    src = inputs[0]
    ext = f.ext_of(src)
    out = ctx.workdir / f"out{f.split_name(src.name)[1]}"
    sets = {k: params[k] for k in FIELDS if params.get(k)}
    if params["remove"] == "nothing" and not sets:
        raise ConversionError("nothing to change: pick what to remove or fill in a field")
    if ext in EXIFTOOL_TYPES:
        shutil.copyfile(src, out)
        cmd: list[str | Path] = ["exiftool", "-q", "-m", "-overwrite_original"]
        if params["remove"] == "everything":
            # Keep what changes how the picture looks: its rotation and colour profile.
            cmd += ["-all=", "-tagsfromfile", "@", "-Orientation", "-ICC_Profile"]
        elif params["remove"] == "location":
            cmd += ["-gps:all=", "-xmp-exif:gps*=", "-xmp:Location*="]
        for field, value in sets.items():
            cmd += [f"-{tag}={value}" for tag in EXIF_TAGS[field]]
        if len(cmd) > 4:
            ctx.run([*cmd, out])
        if ext == "pdf":
            # exiftool edits PDFs as appended updates that can be undone; rewrite it so the
            # removed metadata is really gone.
            clean = ctx.workdir / "clean.pdf"
            ctx.run(["qpdf", "--linearize", out, clean])
            return clean
        return out
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", src,
           "-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-c", "copy"]
    if params["remove"] == "everything":
        cmd += ["-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1"]
    elif params["remove"] == "location":
        for tag in LOCATION_TAGS_FFMPEG:
            cmd += ["-metadata", f"{tag}="]
    for field, value in sets.items():
        cmd += ["-metadata", f"{FFMPEG_TAGS[field]}={value}"]
    if ext in ("mp4", "mov", "m4v", "m4a"):
        cmd += ["-movflags", "+faststart"]
    ctx.run([*cmd, out])
    return out


def _suffix(params: Params) -> str:
    return {"everything": "no metadata", "location": "no location"}.get(params["remove"], "edited")


def _hint(field: str):
    return lambda files: current(files[0]).get(field, "") if len(files) == 1 else ""


TOOL = Tool(
    "metadata", "Metadata", done="Metadata updated",
    sources=EXIFTOOL_TYPES | MEDIA_TYPES,
    engine=edit, suffix=_suffix, icon="document-properties", button="Save copy",
    params=(
        Param("remove", "Remove", "choice", "everything", "which metadata to strip",
              choices=REMOVE),
        Param("title", "Title", "str", "", "set the title (blank: keep)",
              placeholder=_hint("title")),
        Param("author", "Author", "str", "", "set the author/artist (blank: keep)",
              placeholder=_hint("author")),
        Param("comment", "Comment", "str", "", "set a comment (blank: keep)",
              placeholder=_hint("comment")),
    ),
    help="strip personal data (e.g. where a photo was taken) or set title/author",
)

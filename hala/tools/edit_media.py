"""Audio and video edits with ffmpeg: normalize loudness, trim, split, join."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from .. import formats as f
from ..engines import media
from ..runner import Context, ConversionError
from . import Param, Params, Tool, format_time

MEDIA = frozenset({*f.VIDEO_IN, *f.AUDIO_IN})
VIDEO = frozenset(f.VIDEO_IN)
AUDIO = frozenset(f.AUDIO_IN)
# Containers that keep a copied video stream as-is; others get re-wrapped in MKV.
KEEP_CONTAINER = ("mp4", "mov", "m4v", "mkv", "webm")
AUDIO_FOR_CONTAINER = {"webm": ["-c:a", "libopus", "-b:a", "160k"]}
DEFAULT_AUDIO = ["-c:a", "aac", "-b:a", "192k"]


def _halves(ctx: Context) -> tuple[Context, Context]:
    """Contexts whose progress maps to the first and second half of the job."""
    return (replace(ctx, on_progress=lambda frac: ctx.progress(frac / 2)),
            replace(ctx, on_progress=lambda frac: ctx.progress(0.5 + frac / 2)))


def _ffmpeg_cmd(*args: str | Path) -> list[str | Path]:
    return ["ffmpeg", "-hide_banner", "-nostdin", "-y", *args]


def _audio_out(ext: str) -> tuple[str, list[str]]:
    """Output extension and codec flags for re-encoded audio of type ext."""
    return (ext, media.AUDIO_CODECS[ext]) if ext in media.AUDIO_CODECS \
        else ("m4a", media.AUDIO_CODECS["m4a"])


def _require_audio(info: dict) -> None:
    if not any(s.get("codec_type") == "audio" for s in info.get("streams", [])):
        raise ConversionError("there's no sound in this file")


# --- normalize ------------------------------------------------------------------------------

def normalize(ctx: Context, inputs: list[Path], params: Params) -> Path:
    """EBU R128 two-pass loudnorm: measure, then apply linearly (no pumping)."""
    src = inputs[0]
    info = media.probe(ctx, src)
    _require_audio(info)
    duration = media._duration(info)
    target = params["loudness"]
    base = f"loudnorm=I={target}:TP=-1.5:LRA=11"
    first, second = _halves(ctx)
    _out, err, _code = first.run_full(_ffmpeg_cmd(
        "-i", src, "-map", "0:a:0", "-af", f"{base}:print_format=json", "-f", "null", "-"))
    blocks = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", err)
    if not blocks:
        raise ConversionError("couldn't measure the loudness")
    m = json.loads(blocks[-1])
    if m["input_i"] in ("-inf", "inf"):
        raise ConversionError("this recording is silent")
    rate = next((s.get("sample_rate") for s in info["streams"]
                 if s.get("codec_type") == "audio"), "48000")
    chain = (f"{base}:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
             f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
             f":offset={m['target_offset']}:linear=true,aresample={rate}")
    ext = f.ext_of(src)
    if ext in AUDIO:
        out_ext, codec = _audio_out(ext)
        out = ctx.workdir / f"out.{out_ext}"
        media._ffmpeg(second, ["-i", src, "-map", "0:a:0", "-map_metadata", "0", "-af", chain,
                               *codec, out], duration)
        return out
    out_ext = ext if ext in KEEP_CONTAINER else "mkv"
    out = ctx.workdir / f"out.{out_ext}"
    media._ffmpeg(second, ["-i", src, "-map", "0:v", "-map", "0:a:0", "-map_metadata", "0",
                           "-c:v", "copy", "-af", chain,
                           *AUDIO_FOR_CONTAINER.get(out_ext, DEFAULT_AUDIO), out], duration)
    return out


# --- trim -----------------------------------------------------------------------------------

SILENCE = ("silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.1,"
           "areverse,silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.1,"
           "areverse")


def trim(ctx: Context, inputs: list[Path], params: Params) -> Path:
    src = inputs[0]
    info = media.probe(ctx, src)
    duration = media._duration(info)
    start = params["start"] or 0.0
    end = params["end"] if params["end"] else duration
    if duration and end > duration:
        end = duration
    if start >= end:
        raise ConversionError(f"the start ({format_time(start)}) has to be before the end "
                              f"({format_time(end)})")
    if not params["start"] and not params["end"] and not params.get("silence"):
        raise ConversionError("nothing to trim: set a start, an end or remove silence")
    ext = f.ext_of(src)
    span = ["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}"]
    if ext in AUDIO:
        out_ext, codec = _audio_out(ext)
        out = ctx.workdir / f"out.{out_ext}"
        filters = ["-af", SILENCE] if params.get("silence") else []
        media._ffmpeg(ctx, [*span, "-i", src, "-map", "0:a:0", "-map_metadata", "0", *filters,
                            *codec, out], end - start)
        return out
    if params.get("precise", True):
        out_ext = ext if ext in media.VIDEO_CODECS else "mp4"
        out = ctx.workdir / f"out.{out_ext}"
        media._ffmpeg(ctx, [*span, "-i", src, "-map", "0:v:0", "-map", "0:a?",
                            "-map_metadata", "0", *media.EVEN_SIZE,
                            *media.VIDEO_CODECS[out_ext], out], end - start)
        return out
    # Fast: no re-encode, so the cut snaps to the keyframe at or before the start.
    out_ext = ext if ext in KEEP_CONTAINER else "mkv"
    out = ctx.workdir / f"out.{out_ext}"
    media._ffmpeg(ctx, [*span, "-i", src, "-map", "0", "-c", "copy",
                        "-avoid_negative_ts", "make_zero", out], end - start)
    return out


# --- split ----------------------------------------------------------------------------------

def split(ctx: Context, inputs: list[Path], params: Params) -> Path:
    """Equal parts or fixed-length pieces.

    Audio and fast video splits are stream copies (video cuts then land on keyframes, which
    can be seconds apart); a precise video split re-encodes with a keyframe forced at every cut.
    """
    src = inputs[0]
    duration = media._duration(media.probe(ctx, src))
    if duration <= 0:
        raise ConversionError("can't tell how long this file is")
    every = params["every"] or duration / max(2, params["parts"])
    if every >= duration:
        raise ConversionError(f"the file is only {format_time(duration)} long")
    folder = ctx.workdir / "split"
    folder.mkdir()
    ext = f.split_name(src.name)[1].lower()
    segment = ["-f", "segment", "-segment_time", f"{every:.3f}", "-reset_timestamps", "1"]
    if f.ext_of(src) in VIDEO and params.get("precise", True):
        out_ext = f.ext_of(src) if f.ext_of(src) in media.VIDEO_CODECS else "mp4"
        ext = "." + out_ext
        # -movflags belongs to each piece's muxer, not to the segment muxer around them.
        codecs = [a for a in media.VIDEO_CODECS[out_ext] if a not in ("-movflags", "+faststart")]
        # With B-frames the forced keyframe can be stamped a hair before the boundary, and the
        # segmenter only cuts at keyframes at or after it: allow it a fraction of a frame early.
        media._ffmpeg(ctx, ["-i", src, "-map", "0:v:0", "-map", "0:a?", *media.EVEN_SIZE,
                            *codecs, "-force_key_frames", f"expr:gte(t,n_forced*{every:.3f})",
                            *segment, "-segment_time_delta", "0.05",
                            folder / f"part%03d{ext}"], duration)
    else:
        media._ffmpeg(ctx, ["-i", src, "-map", "0", "-c", "copy", *segment,
                            folder / f"part%03d{ext}"], duration)
    stem = f.stem_of(src)
    pieces = sorted(folder.iterdir())
    for i, piece in enumerate(pieces, start=1):
        piece.rename(folder / f"{stem} part {i}{ext}")
    return folder


# --- join -----------------------------------------------------------------------------------

def _signature(info: dict) -> tuple:
    """What has to match for a lossless join."""
    sig = []
    for s in info.get("streams", []):
        if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic"):
            sig.append(("v", s.get("codec_name"), s.get("width"), s.get("height"),
                        s.get("pix_fmt"), s.get("r_frame_rate")))
        elif s.get("codec_type") == "audio":
            sig.append(("a", s.get("codec_name"), s.get("sample_rate"), s.get("channels")))
    return tuple(sig)


def join(ctx: Context, inputs: list[Path], params: Params) -> Path:
    kinds = {f.ext_of(p) in VIDEO for p in inputs}
    if len(kinds) > 1:
        raise ConversionError("join either videos or audio files, not both at once")
    is_video = kinds.pop()
    infos = [media.probe(ctx, p) for p in inputs]
    total = sum(media._duration(i) for i in infos)
    exts = {f.ext_of(p) for p in inputs}
    if len(exts) == 1 and len({_signature(i) for i in infos}) == 1:
        return _join_copy(ctx, inputs, total)
    return _join_encode(ctx, inputs, infos, total, is_video)


def _join_copy(ctx: Context, inputs: list[Path], total: float) -> Path:
    if any("\n" in str(p) or "\r" in str(p) for p in inputs):
        # A line break would end the entry and start a new directive in the list file.
        raise ConversionError("can't join files with a line break in their name")
    listing = ctx.workdir / "list.txt"
    # concat demuxer syntax: single-quoted paths, with ' written as '\''.
    listing.write_text("".join("file '" + str(p).replace("'", "'\\''") + "'\n" for p in inputs))
    out = ctx.workdir / f"out{f.split_name(inputs[0].name)[1].lower()}"
    media._ffmpeg(ctx, ["-f", "concat", "-safe", "0", "-i", listing, "-map", "0", "-c", "copy",
                        out], total)
    return out


def _join_encode(ctx: Context, inputs: list[Path], infos: list[dict], total: float,
                 is_video: bool) -> Path:
    args: list[str | Path] = []
    for p in inputs:
        args += ["-i", p]
    if not is_video:
        out_ext, codec = _audio_out(f.ext_of(inputs[0]))
        chain = "".join(f"[{i}:a:0]" for i in range(len(inputs)))
        out = ctx.workdir / f"out.{out_ext}"
        media._ffmpeg(ctx, [*args, "-filter_complex",
                            f"{chain}concat=n={len(inputs)}:v=0:a=1[a]", "-map", "[a]", *codec,
                            out], total)
        return out
    # Everything is fitted into the first video's frame (letterboxed, not stretched).
    first = next(s for s in infos[0]["streams"] if s.get("codec_type") == "video")
    w, h = first["width"] // 2 * 2, first["height"] // 2 * 2
    fps = first.get("r_frame_rate", "30/1")
    parts, extra = [], len(inputs)
    for i, info in enumerate(infos):
        parts.append(f"[{i}:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v{i}]")
        if any(s.get("codec_type") == "audio" for s in info.get("streams", [])):
            parts.append(f"[{i}:a:0]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
        else:  # silent clip: give it a matching stretch of silence
            args += ["-f", "lavfi", "-t", f"{media._duration(info):.3f}",
                     "-i", "anullsrc=r=48000:cl=stereo"]
            parts.append(f"[{extra}:a:0]anull[a{i}]")
            extra += 1
    chain = "".join(f"[v{i}][a{i}]" for i in range(len(inputs)))
    graph = ";".join(parts) + f";{chain}concat=n={len(inputs)}:v=1:a=1[v][a]"
    out = ctx.workdir / "out.mp4"
    media._ffmpeg(ctx, [*args, "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
                        *media.VIDEO_CODECS["mp4"], out], total)
    return out


NORMALIZE = Tool(
    "normalize", "Normalize audio", short="Normalize", done="Loudness evened out",
    sources=MEDIA, engine=normalize,
    suffix=lambda p: "normalized", icon="audio-volume-high",
    params=(Param("loudness", "Loudness", "float", -16.0,
                  "target in LUFS: -14 streaming, -16 podcasts, -23 broadcast", unit="LUFS"),),
    help="even out the volume to a standard loudness",
)
TRIM = Tool(
    "trim", "Trim", done="Trimmed",
    sources=MEDIA, engine=trim, suffix=lambda p: "trimmed", icon="edit-cut",
    params=(
        Param("start", "Start", "time", 0.0, "where to start, e.g. 0:05 or 1:02:03.5"),
        Param("end", "End", "time", None, "where to end (blank: the end of the file)"),
        Param("precise", "Frame-accurate", "bool", True,
              "re-encode for an exact cut (off: instant, but cuts at a keyframe)",
              only_for=VIDEO),
        Param("silence", "Remove silence", "bool", False,
              "also cut silence at the start and end", only_for=AUDIO),
    ),
    help="keep only part of a recording or video",
)
SPLIT = Tool(
    "split", "Split", done="Split",
    sources=MEDIA, engine=split, suffix=lambda p: "split", icon="split",
    params=(
        Param("parts", "Parts", "int", 2, "how many equal parts"),
        Param("every", "Or every", "time", None, "pieces this long instead, e.g. 10:00"),
        Param("precise", "Frame-accurate", "bool", True,
              "re-encode for exact cuts (off: instant, but cuts at keyframes)", only_for=VIDEO),
    ),
    help="cut into equal parts or fixed-length pieces",
)
JOIN = Tool(
    "join", "Join", done="Joined",
    sources=MEDIA, engine=join, combine=True, min_files=2,
    suffix=lambda p: "joined", icon="merge",
    help="one video (or recording) from several, in the order given",
)
